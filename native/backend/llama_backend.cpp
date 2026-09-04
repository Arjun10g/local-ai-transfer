#include "llama_backend.hpp"

#include <stdexcept>

#ifdef LAE_ENABLE_LLAMA_CPP
#include "llama.h"

#include <algorithm>
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
};

namespace {
bool abort_callback(void* data) {
  const auto* cancellation = static_cast<const Cancellation*>(data);
  return cancellation && *cancellation && (*cancellation)->load();
}
}

LlamaBackend::LlamaBackend() : impl_(new Impl()) {}
LlamaBackend::~LlamaBackend() { shutdown(); delete impl_; }
std::string LlamaBackend::id() const { return "llama.cpp/3581ba0c/cpu"; }

void LlamaBackend::initialize(const BackendConfig& config) {
  if (config.model_path.empty()) throw std::invalid_argument("model path is required");
  llama_backend_init();
  auto model_params = llama_model_default_params();
  model_params.n_gpu_layers = 0;
  model_params.check_tensors = true;
  model_params.load_mtp = false;
  impl_->model = llama_model_load_from_file(config.model_path.c_str(), model_params);
  if (!impl_->model) throw std::runtime_error("llama model load failed");
  auto context_params = llama_context_default_params();
  context_params.n_ctx = config.context_tokens == 0 ? 8192 : config.context_tokens;
  context_params.n_batch = std::min<uint32_t>(context_params.n_ctx, 512);
  context_params.n_ubatch = context_params.n_batch;
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
  std::vector<llama_token> prompt(4096);
  int32_t count = llama_tokenize(vocab, request.prompt.c_str(), static_cast<int32_t>(request.prompt.size()), prompt.data(), static_cast<int32_t>(prompt.size()), true, false);
  if (count < 0) { prompt.resize(static_cast<size_t>(-count)); count = llama_tokenize(vocab, request.prompt.c_str(), static_cast<int32_t>(request.prompt.size()), prompt.data(), -count, true, false); }
  if (count <= 0) throw std::runtime_error("llama tokenization failed");
  prompt.resize(static_cast<size_t>(count));
  if (prompt.size() >= impl_->context_tokens) throw std::invalid_argument("context limit exceeded");
  llama_batch batch = llama_batch_init(static_cast<int32_t>(prompt.size()), 0, 1);
  for (size_t i = 0; i < prompt.size(); ++i) {
    batch.token[i] = prompt[i]; batch.pos[i] = static_cast<llama_pos>(i); batch.n_seq_id[i] = 1; batch.seq_id[i][0] = 0; batch.logits[i] = (i + 1 == prompt.size());
  }
  if (llama_decode(impl_->context, batch) != 0) { llama_batch_free(batch); throw std::runtime_error("llama prefill failed"); }
  llama_batch_free(batch);
  GenerationResult result;
  for (unsigned generated = 0; generated < request.max_tokens; ++generated) {
    if (cancellation->load()) { result.finish_reason = "cancelled"; return result; }
    const llama_token token = llama_sampler_sample(impl_->sampler, impl_->context, -1);
    if (llama_vocab_is_eog(vocab, token)) { result.finish_reason = "stop"; return result; }
    char piece[1024]; const int32_t piece_size = llama_token_to_piece(vocab, token, piece, sizeof(piece), 0, false);
    if (piece_size < 0 || !sink(std::string(piece, static_cast<size_t>(piece_size)))) { result.finish_reason = "cancelled"; return result; }
    ++result.generated_tokens;
    llama_batch next = llama_batch_init(1, 0, 1);
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
  llama_backend_free();
}
}  // namespace lae
#else
namespace lae {
struct LlamaBackend::Impl {};
LlamaBackend::LlamaBackend() : impl_(new Impl()) {}
LlamaBackend::~LlamaBackend() { delete impl_; }
std::string LlamaBackend::id() const { return "llama.cpp/3581ba0c/cpu-disabled"; }
void LlamaBackend::initialize(const BackendConfig&) { throw std::runtime_error("real backend disabled; configure LAE_ENABLE_LLAMA_CPP=ON"); }
GenerationResult LlamaBackend::generate(const GenerationRequest&, const Cancellation&, const TokenSink&) { throw std::runtime_error("real backend disabled"); }
void LlamaBackend::reset() {}
void LlamaBackend::shutdown() {}
}  // namespace lae
#endif
