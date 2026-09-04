#include "llama_backend.hpp"
#include "llama_chat_template.hpp"
#include "model_validation/model_validator.hpp"

#include <algorithm>
#include <stdexcept>

namespace lae {
bool context_budget_fits(size_t prompt_tokens, unsigned max_tokens, unsigned context_tokens) {
  return context_tokens != 0 && prompt_tokens <= context_tokens && max_tokens <= context_tokens - prompt_tokens;
}

ContextBatchConfig context_batch_config(unsigned context_tokens) {
  const unsigned n_ctx = context_tokens == 0 ? 8192u : context_tokens;
  return ContextBatchConfig{n_ctx, n_ctx, std::min(n_ctx, 512u)};
}
}  // namespace lae

#ifdef LAE_ENABLE_LLAMA_CPP
#include "llama.h"
#include "ggml-backend.h"

#include <cstring>
#include <memory>
#include <thread>
#include <vector>

namespace lae {
struct LlamaBackend::Impl {
  llama_model* model = nullptr;
  llama_context* context = nullptr;
  llama_sampler* sampler = nullptr;
  unsigned context_tokens = 8192;
  Cancellation cancellation;
  bool backend_initialized = false;
  PinnedChatTemplate chat_template;
  ggml_backend_dev_t selected_device = nullptr;
  std::string active_backend = "cpu";
  std::shared_ptr<ModelValidationLease> model_lease;
};

namespace {
bool abort_callback(void* data) {
  const auto* cancellation = static_cast<const Cancellation*>(data);
  return cancellation && *cancellation && (*cancellation)->load();
}

}

LlamaBackend::LlamaBackend() : impl_(new Impl()) {}
LlamaBackend::~LlamaBackend() { shutdown(); delete impl_; }
std::string LlamaBackend::id() const {
  return std::string("llama.cpp/3581ba0c/") + impl_->active_backend;
}
std::string LlamaBackend::runtime_info_json() const {
  if (!impl_->context) return "{}";
  return std::string("{\"context_tokens\":") + std::to_string(llama_n_ctx(impl_->context)) +
         ",\"n_batch\":" + std::to_string(llama_n_batch(impl_->context)) +
         ",\"n_ubatch\":" + std::to_string(llama_n_ubatch(impl_->context)) + "}";
}

void LlamaBackend::initialize(const BackendConfig& config) {
  if (config.model_path.empty()) throw std::invalid_argument("model path is required");
  if (!config.model_lease || config.model_lease->canonical_path() != config.model_path ||
      !config.model_lease->unchanged()) {
    throw std::runtime_error("model validation lease is missing, mismatched, or stale");
  }
  impl_->model_lease = config.model_lease;
#if defined(LAE_ENABLE_LLAMA_VULKAN) && defined(LAE_ENABLE_LLAMA_CUDA)
  throw std::runtime_error("product cannot be built with both CUDA and Vulkan");
#elif defined(LAE_ENABLE_LLAMA_VULKAN)
  if (config.backend_profile != "intel-vulkan" && config.backend_profile != "cpu") throw std::invalid_argument("unsupported compiled backend profile");
#elif defined(LAE_ENABLE_LLAMA_CUDA)
  if (config.backend_profile != "cuda" && config.backend_profile != "cpu") throw std::invalid_argument("unsupported compiled backend profile");
#else
  if (config.backend_profile == "intel-vulkan") throw std::runtime_error("intel-vulkan requested but product was not built with LAE_ENABLE_LLAMA_VULKAN");
  if (config.backend_profile == "cuda") throw std::runtime_error("cuda requested but product was not built with LAE_ENABLE_LLAMA_CUDA");
  if (config.backend_profile != "cpu") throw std::invalid_argument("unsupported compiled backend profile");
#endif
#ifdef LAE_ENABLE_LLAMA_VULKAN
  if (config.backend_profile == "intel-vulkan" && (config.gpu_layers < 1 || config.gpu_layers > 99)) throw std::invalid_argument("intel-vulkan gpu_layers must be between 1 and 99");
  if (config.backend_profile == "intel-vulkan") {
    llama_backend_init();
    impl_->backend_initialized = true;
    ggml_backend_dev_t selected = nullptr;
    for (size_t index = 0; index < ggml_backend_dev_count(); ++index) {
      ggml_backend_dev_t device = ggml_backend_dev_get(index);
      if (ggml_backend_dev_type(device) != GGML_BACKEND_DEVICE_TYPE_IGPU) continue;
      const char* name = ggml_backend_dev_name(device);
      const char* description = ggml_backend_dev_description(device);
      const bool exact_name = name && config.vulkan_device_name == name;
      const bool exact_description = description && config.vulkan_device_name == description;
      if (!exact_name && !exact_description) continue;
      if (selected) throw std::runtime_error("multiple Vulkan devices match the exact configured name");
      selected = device;
    }
    if (!selected) throw std::runtime_error("exact configured Vulkan integrated device is unavailable");
    impl_->selected_device = selected;
    impl_->active_backend = "intel-vulkan";
  }
#endif
#ifdef LAE_ENABLE_LLAMA_CUDA
  if (config.backend_profile == "cuda" && (config.gpu_layers < 1 || config.gpu_layers > 99)) throw std::invalid_argument("cuda gpu_layers must be between 1 and 99");
  if (config.backend_profile == "cuda") {
    llama_backend_init();
    impl_->backend_initialized = true;
    ggml_backend_dev_t selected = nullptr;
    for (size_t index = 0; index < ggml_backend_dev_count(); ++index) {
      ggml_backend_dev_t device = ggml_backend_dev_get(index);
      if (ggml_backend_dev_type(device) != GGML_BACKEND_DEVICE_TYPE_GPU) continue;
      const char* name = ggml_backend_dev_name(device);
      const char* description = ggml_backend_dev_description(device);
      const bool exact_name = name && config.cuda_device_name == name;
      const bool exact_description = description && config.cuda_device_name == description;
      if (!exact_name && !exact_description) continue;
      if (selected) throw std::runtime_error("multiple CUDA devices match the exact configured name");
      selected = device;
    }
    if (!selected) throw std::runtime_error("exact configured CUDA device is unavailable");
    impl_->selected_device = selected;
    impl_->active_backend = "cuda";
  }
#endif
  if (!impl_->backend_initialized) {
    llama_backend_init();
    impl_->backend_initialized = true;
  }
  auto model_params = llama_model_default_params();
#if defined(LAE_ENABLE_LLAMA_VULKAN) || defined(LAE_ENABLE_LLAMA_CUDA)
  ggml_backend_dev_t device_list[2] = {nullptr, nullptr};
  if (impl_->selected_device) { device_list[0] = impl_->selected_device; model_params.devices = device_list; }
#endif
  model_params.n_gpu_layers = (config.backend_profile == "intel-vulkan" || config.backend_profile == "cuda") ? static_cast<int32_t>(config.gpu_layers) : 0;
  model_params.check_tensors = true;
  model_params.load_mtp = false;
  const std::string load_path = impl_->model_lease->authorized_load_path(config.model_path);
  if (load_path.empty()) throw std::runtime_error("model validation lease became stale before backend load");
  impl_->model = llama_model_load_from_file(load_path.c_str(), model_params);
  if (!impl_->model) throw std::runtime_error("llama model load failed");
  if (!impl_->model_lease->unchanged()) {
    llama_model_free(impl_->model); impl_->model = nullptr;
    throw std::runtime_error("model identity changed during backend load");
  }
  const char* embedded_template = llama_model_chat_template(impl_->model, nullptr);
  if (!embedded_template || !*embedded_template) throw std::runtime_error("llama chat template unavailable; raw prompt mode is not accepted");
  impl_->chat_template.load(embedded_template);
  auto context_params = llama_context_default_params();
  const auto batch_config = context_batch_config(config.context_tokens);
  context_params.n_ctx = batch_config.n_ctx;
  context_params.n_batch = batch_config.n_batch;
  context_params.n_ubatch = batch_config.n_ubatch;
  context_params.n_seq_max = 1;
  context_params.n_threads = static_cast<int32_t>(std::max(1u, std::thread::hardware_concurrency()));
  context_params.n_threads_batch = context_params.n_threads;
  context_params.abort_callback = abort_callback;
  context_params.abort_callback_data = &impl_->cancellation;
  impl_->context = llama_init_from_model(impl_->model, context_params);
  if (!impl_->context) throw std::runtime_error("llama context creation failed");
  impl_->context_tokens = context_params.n_ctx;
  auto sampler_params = llama_sampler_chain_default_params();
  impl_->sampler = llama_sampler_chain_init(sampler_params);
  if (!impl_->sampler) throw std::runtime_error("llama sampler creation failed");
  llama_sampler_chain_add(impl_->sampler, llama_sampler_init_greedy());
}

GenerationResult LlamaBackend::generate(const GenerationRequest& request,
                                        const Cancellation& cancellation,
                                        const TokenSink& sink) {
  if (!impl_->model || !impl_->context || !impl_->sampler) throw std::runtime_error("llama backend is not initialized");
  impl_->cancellation = cancellation;
  reset();
  const auto* vocab = llama_model_get_vocab(impl_->model);
  const std::string rendered = impl_->chat_template.render(request.messages, request.tools, request.enable_thinking);
  std::vector<llama_token> prompt(4096);
  int32_t count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(rendered.size()), prompt.data(), static_cast<int32_t>(prompt.size()), true, false);
  if (count < 0) { prompt.resize(static_cast<size_t>(-count)); count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(rendered.size()), prompt.data(), -count, true, false); }
  if (count <= 0) throw std::runtime_error("llama tokenization failed");
  prompt.resize(static_cast<size_t>(count));
  if (!context_budget_fits(prompt.size(), request.max_tokens, impl_->context_tokens)) throw std::invalid_argument("context limit exceeded");
  llama_batch batch = llama_batch_init(static_cast<int32_t>(prompt.size()), 0, 1);
  batch.n_tokens = static_cast<int32_t>(prompt.size());
  for (size_t i = 0; i < prompt.size(); ++i) {
    batch.token[i] = prompt[i]; batch.pos[i] = static_cast<llama_pos>(i); batch.n_seq_id[i] = 1; batch.seq_id[i][0] = 0; batch.logits[i] = (i + 1 == prompt.size());
  }
  if (llama_decode(impl_->context, batch) != 0) { llama_batch_free(batch); throw std::runtime_error("llama prefill failed"); }
  llama_batch_free(batch);
  GenerationResult result;
  result.prompt_tokens = static_cast<unsigned>(prompt.size());
  for (unsigned generated = 0; generated < request.max_tokens; ++generated) {
    if (cancellation->load()) { result.finish_reason = "cancelled"; return result; }
    const llama_token token = llama_sampler_sample(impl_->sampler, impl_->context, -1);
    if (llama_vocab_is_eog(vocab, token)) { result.finish_reason = "stop"; return result; }
    char piece[1024]; const int32_t piece_size = llama_token_to_piece(vocab, token, piece, sizeof(piece), 0, false);
    if (piece_size < 0 || !sink(std::string(piece, static_cast<size_t>(piece_size)))) { result.finish_reason = "cancelled"; return result; }
    ++result.generated_tokens;
    llama_batch next = llama_batch_init(1, 0, 1);
    next.n_tokens = 1;
    next.token[0] = token; next.pos[0] = static_cast<llama_pos>(prompt.size() + generated); next.n_seq_id[0] = 1; next.seq_id[0][0] = 0; next.logits[0] = true;
    if (llama_decode(impl_->context, next) != 0) { llama_batch_free(next); throw std::runtime_error("llama decode failed"); }
    llama_batch_free(next);
  }
  result.finish_reason = "length";
  return result;
}

void LlamaBackend::reset() {
  if (impl_->context) llama_memory_clear(llama_get_memory(impl_->context), true);
  if (impl_->sampler) llama_sampler_reset(impl_->sampler);
}

void LlamaBackend::shutdown() {
  if (!impl_) return;
  if (impl_->sampler) { llama_sampler_free(impl_->sampler); impl_->sampler = nullptr; }
  if (impl_->context) { llama_free(impl_->context); impl_->context = nullptr; }
  if (impl_->model) { llama_model_free(impl_->model); impl_->model = nullptr; }
  impl_->model_lease.reset();
  if (impl_->backend_initialized) { llama_backend_free(); impl_->backend_initialized = false; }
}
}  // namespace lae
#else
namespace lae {
struct LlamaBackend::Impl {};
LlamaBackend::LlamaBackend() : impl_(new Impl()) {}
LlamaBackend::~LlamaBackend() { delete impl_; }
std::string LlamaBackend::id() const { return "llama.cpp/3581ba0c/cpu-disabled"; }
std::string LlamaBackend::runtime_info_json() const { return "{}"; }
void LlamaBackend::initialize(const BackendConfig&) { throw std::runtime_error("real backend disabled; configure LAE_ENABLE_LLAMA_CPP=ON"); }
GenerationResult LlamaBackend::generate(const GenerationRequest&, const Cancellation&, const TokenSink&) { throw std::runtime_error("real backend disabled"); }
void LlamaBackend::reset() {}
void LlamaBackend::shutdown() {}
}  // namespace lae
#endif
