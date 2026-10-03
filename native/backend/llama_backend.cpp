#include "llama_backend.hpp"
#include "llama_chat_template.hpp"
#include "model_validation/model_validator.hpp"

#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace lae {
bool context_budget_fits(size_t prompt_tokens, unsigned max_tokens, unsigned context_tokens) {
  return context_tokens != 0 && prompt_tokens <= context_tokens && max_tokens <= context_tokens - prompt_tokens;
}

ContextBatchConfig context_batch_config(unsigned context_tokens) {
  const unsigned n_ctx = context_tokens == 0 ? 8192u : context_tokens;
  return ContextBatchConfig{n_ctx, n_ctx, std::min(n_ctx, 512u)};
}

std::vector<int32_t> ngram_draft(const std::vector<int32_t>& history,
                                 size_t max_ngram, size_t max_draft) {
  constexpr size_t kMinNgram = 2;
  if (max_draft == 0 || max_ngram < kMinNgram || history.size() <= kMinNgram) return {};
  const size_t longest = std::min(max_ngram, history.size() - 1);
  for (size_t n = longest; n >= kMinNgram; --n) {
    const size_t tail = history.size() - n;  // start of the suffix being matched
    // An earlier occurrence must leave at least one token after it to propose.
    for (size_t start = tail; start-- > 0;) {
      if (!std::equal(history.begin() + static_cast<std::ptrdiff_t>(start),
                      history.begin() + static_cast<std::ptrdiff_t>(start + n),
                      history.begin() + static_cast<std::ptrdiff_t>(tail))) continue;
      const size_t from = start + n;
      const size_t count = std::min(max_draft, history.size() - from);
      return std::vector<int32_t>(history.begin() + static_cast<std::ptrdiff_t>(from),
                                  history.begin() + static_cast<std::ptrdiff_t>(from + count));
    }
  }
  return {};
}

std::string neutralize_control_text(std::string text, const std::vector<std::string>& control_texts) {
  constexpr const char* kZeroWidthSpace = "\xE2\x80\x8B";
  for (const auto& control : control_texts) {
    if (control.size() < 2) continue;
    size_t at = 0;
    while ((at = text.find(control, at)) != std::string::npos) {
      text.insert(at + 1, kZeroWidthSpace);
      at += 1 + 3;  // past the first character and the inserted space
    }
  }
  return text;
}

std::string defuse_untrusted_text(std::string text, bool assistant_authored,
                                  const std::vector<std::string>& control_texts,
                                  const std::vector<std::string>& tag_texts) {
  text = neutralize_control_text(std::move(text), control_texts);
  if (!assistant_authored) text = neutralize_control_text(std::move(text), tag_texts);
  return text;
}

size_t reusable_prefix_tokens(const std::vector<int32_t>& state,
                              const std::vector<int32_t>& prompt) {
  // Strictly fewer, so at least one token is left to decode and read logits
  // from; equal or longer would need a rewind the recurrent state cannot do.
  if (state.empty() || state.size() >= prompt.size()) return 0;
  if (!std::equal(state.begin(), state.end(), prompt.begin())) return 0;
  return state.size();
}
}  // namespace lae

#ifdef LAE_ENABLE_LLAMA_CPP
#include "llama.h"
#include "ggml-backend.h"

#include <atomic>
#include <cstring>
#include <iostream>
#include <memory>
#include <thread>
#include <vector>

namespace lae {
struct LlamaBackend::Impl {
  // Tokens the retained context currently holds, and the conversation they
  // belong to. `state_valid` is false whenever the context's contents are not
  // exactly `state_tokens` -- including after any failed decode.
  std::vector<llama_token> state_tokens;
  std::string state_key;
  bool state_valid = false;
  // Everything /metrics reads is atomic. `generate` runs outside the Engine's
  // mutex while `runtime_info_json` runs under it on another thread, so a plain
  // read of `state_tokens.size()` could race a reallocation. The vector itself
  // stays generate-only; `publish()` copies its size out after each change.
  std::atomic<unsigned> last_reused{0};
  // Diagnostics only, and deliberately counts rather than content: how much of
  // the retained state the new prompt still agreed with. A value just short of
  // `retained_prompt_tokens` means the two renderings diverge near the end.
  std::atomic<unsigned> last_common_prefix{0};
  std::atomic<unsigned> last_state_len{0};
  std::atomic<size_t> retained{0};
  void publish() { retained.store(state_valid ? state_tokens.size() : 0); }
  // Speculation. `spec_max` is what the context can actually roll back, which
  // may be less than requested (upstream clamps `n_rs_seq` to 0 for an
  // architecture that does not opt in), and is the only value drafting trusts.
  unsigned spec_max = 0;
  std::atomic<unsigned long long> spec_drafted{0}, spec_accepted{0}, spec_steps{0};
  // Text of every control token in the vocabulary (<|im_start|>, <|im_end|>, ...),
  // collected once at load for `neutralize_control_text`.
  std::vector<std::string> control_texts;
  // Text of the USER_DEFINED tags (<tool_call>, <tool_response>, <think>, ...),
  // which the tokenizer matches even with special parsing off.
  std::vector<std::string> tag_texts;
  // A saved copy of the whole sequence state (attention KV + recurrent) taken at
  // the point where the NEXT user turn's prompt is guaranteed to still agree with
  // this one. The live context cannot serve that turn: the chat template re-renders
  // an earlier assistant message without its <think> scaffold, so the new prompt
  // diverges from the live state a few tokens before the generated reply, and the
  // recurrent layers cannot be rewound that far. Restoring this snapshot instead
  // costs one memory copy, where re-reading the history costs minutes on a CPU.
  std::vector<uint8_t> snapshot_blob;
  std::vector<llama_token> snapshot_tokens;
  std::string snapshot_key;
  bool snapshot_valid = false;
  std::atomic<size_t> pub_snapshot_tokens{0}, pub_snapshot_bytes{0};
  std::atomic<unsigned long long> snapshot_restores{0};
  std::atomic<unsigned> last_restored{0};
  void clear_context() {
    if (context) llama_memory_clear(llama_get_memory(context), true);
    if (sampler) llama_sampler_reset(sampler);
    state_tokens.clear(); state_key.clear(); state_valid = false; last_reused = 0; publish();
  }
  void drop_snapshot() {
    std::vector<uint8_t>().swap(snapshot_blob);
    snapshot_tokens.clear(); snapshot_key.clear(); snapshot_valid = false;
    pub_snapshot_tokens = 0; pub_snapshot_bytes = 0;
  }
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
         ",\"n_ubatch\":" + std::to_string(llama_n_ubatch(impl_->context)) +
         ",\"n_threads\":" + std::to_string(llama_n_threads(impl_->context)) +
         ",\"n_threads_batch\":" + std::to_string(llama_n_threads_batch(impl_->context)) +
         ",\"speculate_tokens\":" + std::to_string(impl_->spec_max) +
         ",\"spec_steps\":" + std::to_string(impl_->spec_steps.load()) +
         ",\"spec_drafted\":" + std::to_string(impl_->spec_drafted.load()) +
         ",\"spec_accepted\":" + std::to_string(impl_->spec_accepted.load()) +
         ",\"last_reused_prefix_tokens\":" + std::to_string(impl_->last_reused.load()) +
         ",\"last_common_prefix_tokens\":" + std::to_string(impl_->last_common_prefix.load()) +
         ",\"last_state_tokens\":" + std::to_string(impl_->last_state_len.load()) +
         ",\"retained_prompt_tokens\":" + std::to_string(impl_->retained.load()) +
         ",\"snapshot_tokens\":" + std::to_string(impl_->pub_snapshot_tokens.load()) +
         ",\"snapshot_bytes\":" + std::to_string(impl_->pub_snapshot_bytes.load()) +
         ",\"snapshot_restores\":" + std::to_string(impl_->snapshot_restores.load()) +
         ",\"last_restored_snapshot_tokens\":" + std::to_string(impl_->last_restored.load()) + "}";
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
  {
    const auto* vocab_for_controls = llama_model_get_vocab(impl_->model);
    const int32_t vocab_size = llama_vocab_n_tokens(vocab_for_controls);
    for (int32_t id = 0; id < vocab_size; ++id) {
      const auto attr = llama_vocab_get_attr(vocab_for_controls, id);
      const char* text = llama_vocab_get_text(vocab_for_controls, id);
      if (!text || !*text) continue;
      if (attr & (LLAMA_TOKEN_ATTR_CONTROL | LLAMA_TOKEN_ATTR_UNKNOWN)) impl_->control_texts.emplace_back(text);
      else if (attr & LLAMA_TOKEN_ATTR_USER_DEFINED) impl_->tag_texts.emplace_back(text);
    }
  }
  auto context_params = llama_context_default_params();
  const auto batch_config = context_batch_config(config.context_tokens);
  context_params.n_ctx = batch_config.n_ctx;
  context_params.n_batch = batch_config.n_batch;
  context_params.n_ubatch = batch_config.n_ubatch;
  context_params.n_seq_max = 1;
  context_params.n_rs_seq = config.speculate_tokens;
  context_params.n_threads = static_cast<int32_t>(
      config.threads != 0 ? config.threads : std::max(1u, std::thread::hardware_concurrency()));
  context_params.n_threads_batch = config.threads_batch != 0
      ? static_cast<int32_t>(config.threads_batch) : context_params.n_threads;
  context_params.abort_callback = abort_callback;
  context_params.abort_callback_data = &impl_->cancellation;
  impl_->context = llama_init_from_model(impl_->model, context_params);
  if (!impl_->context) throw std::runtime_error("llama context creation failed");
  impl_->context_tokens = context_params.n_ctx;
  impl_->spec_max = std::min<unsigned>(config.speculate_tokens, llama_n_rs_seq(impl_->context));
  if (config.speculate_tokens != 0 && impl_->spec_max == 0)
    std::cerr << "speculation requested but the context cannot roll back; running without it\n";
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
  const auto* vocab = llama_model_get_vocab(impl_->model);
  // Untrusted text is defused first so that turning special-token parsing ON
  // (below) cannot let content forge a role switch. See neutralize_control_text.
  std::vector<GenerationRequest::ChatMessage> safe_messages = request.messages;
  for (auto& message : safe_messages) {
    const bool assistant_authored = message.role == "assistant";
    message.content = defuse_untrusted_text(std::move(message.content), assistant_authored, impl_->control_texts, impl_->tag_texts);
    message.name = defuse_untrusted_text(std::move(message.name), false, impl_->control_texts, impl_->tag_texts);
    message.tool_call_id = defuse_untrusted_text(std::move(message.tool_call_id), false, impl_->control_texts, impl_->tag_texts);
  }
  std::vector<GenerationRequest::ToolDefinition> safe_tools = request.tools;
  for (auto& tool : safe_tools) {
    tool.name = defuse_untrusted_text(std::move(tool.name), false, impl_->control_texts, impl_->tag_texts);
    tool.description = defuse_untrusted_text(std::move(tool.description), false, impl_->control_texts, impl_->tag_texts);
    tool.parameters_json = defuse_untrusted_text(std::move(tool.parameters_json), false, impl_->control_texts, impl_->tag_texts);
  }
  const std::string rendered = impl_->chat_template.render(safe_messages, safe_tools, request.enable_thinking);
  std::vector<llama_token> prompt(4096);
  int32_t count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(rendered.size()), prompt.data(), static_cast<int32_t>(prompt.size()), true, true);
  if (count < 0) { prompt.resize(static_cast<size_t>(-count)); count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(rendered.size()), prompt.data(), -count, true, true); }
  if (count <= 0) throw std::runtime_error("llama tokenization failed");
  prompt.resize(static_cast<size_t>(count));
  if (!context_budget_fits(prompt.size(), request.max_tokens, impl_->context_tokens)) throw std::invalid_argument("context limit exceeded");
  // Reuse the retained prefix when this prompt extends what the context
  // already holds, so a conversation re-reads only its new tokens. Anything
  // else drops the state whole: the recurrent half cannot be rewound (see
  // `reusable_prefix_tokens`). The sampler is greedy and therefore stateless;
  // a sampler with memory would have to be reset on the reuse path too.
  // An empty key means "no session": every anonymous request would otherwise
  // share one slot, so unrelated conversations could extend each other's state
  // (output-safe, because reuse needs an exact token prefix, but it would let
  // /metrics reveal how much of a guessed prompt matched a stranger's).
  const bool same_conversation = impl_->state_valid && !request.cache_key.empty() &&
                                 impl_->state_key == request.cache_key;
  impl_->last_state_len = static_cast<unsigned>(same_conversation ? impl_->state_tokens.size() : 0);
  impl_->last_common_prefix = static_cast<unsigned>(
      same_conversation ? static_cast<size_t>(std::distance(
                              impl_->state_tokens.begin(),
                              std::mismatch(impl_->state_tokens.begin(), impl_->state_tokens.end(),
                                            prompt.begin(), prompt.end()).first))
                        : 0);
  size_t reused = same_conversation ? reusable_prefix_tokens(impl_->state_tokens, prompt) : 0;
  impl_->last_restored = 0;
  if (reused == 0) {
    // The live context does not serve this prompt (a new user turn lands here).
    // Prefer restoring the snapshot taken at the last conversation boundary.
    const size_t snap = (impl_->snapshot_valid && !request.cache_key.empty() && impl_->snapshot_key == request.cache_key)
                            ? reusable_prefix_tokens(impl_->snapshot_tokens, prompt) : 0;
    impl_->clear_context();
    if (snap > 0) {
      if (llama_state_seq_set_data(impl_->context, impl_->snapshot_blob.data(), impl_->snapshot_blob.size(), 0) != 0) {
        reused = snap;
        impl_->state_tokens = impl_->snapshot_tokens;  // what the context now holds
        impl_->last_restored = static_cast<unsigned>(snap);
        ++impl_->snapshot_restores;
      } else {
        impl_->clear_context();  // a half-restored context is worse than an empty one
        impl_->drop_snapshot();
      }
    } else if (impl_->snapshot_valid && impl_->snapshot_key != request.cache_key) {
      impl_->drop_snapshot();  // another conversation took the context over
    }
  }
  impl_->state_valid = false;  // the context is in flux until the decode lands
  impl_->publish();
  impl_->last_reused = static_cast<unsigned>(reused);

  // Where will the NEXT user turn's prompt still agree with this one? Render a
  // synthetic follow-up and take the common prefix, so this does not depend on
  // how the template treats history. Snapshots are off while speculating: that
  // mode keeps extra recurrent snapshots whose state layout is not ours to copy.
  size_t boundary = 0;
  if (impl_->spec_max == 0 && !request.cache_key.empty()) {
    try {
      auto probe_messages = safe_messages;
      probe_messages.push_back({"assistant", "x", "", ""});
      probe_messages.push_back({"user", "y", "", ""});
      const std::string probe = impl_->chat_template.render(probe_messages, safe_tools, request.enable_thinking);
      size_t common = 0;
      const size_t limit = std::min(rendered.size(), probe.size());
      while (common < limit && rendered[common] == probe[common]) ++common;
      while (common > 0 && common < rendered.size() && (static_cast<unsigned char>(rendered[common]) & 0xC0) == 0x80) --common;  // never inside a UTF-8 sequence
      if (common > 0) {
        std::vector<llama_token> head(4096);
        int32_t head_count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(common), head.data(), static_cast<int32_t>(head.size()), true, true);
        if (head_count < 0) { head.resize(static_cast<size_t>(-head_count)); head_count = llama_tokenize(vocab, rendered.c_str(), static_cast<int32_t>(common), head.data(), -head_count, true, true); }
        if (head_count > 0) {
          head.resize(static_cast<size_t>(head_count));
          // Only tokens that are literally the start of THIS prompt count, so a merge across the cut cannot misalign the snapshot.
          boundary = static_cast<size_t>(std::mismatch(head.begin(), head.end(), prompt.begin(), prompt.end()).first - head.begin());
        }
      }
    } catch (...) { boundary = 0; }  // a template that cannot render the probe simply gets no snapshot
  }

  const auto decode_range = [&](size_t from, size_t to) {
    const size_t count_in_range = to - from;
    llama_batch batch = llama_batch_init(static_cast<int32_t>(count_in_range), 0, 1);
    batch.n_tokens = static_cast<int32_t>(count_in_range);
    for (size_t i = 0; i < count_in_range; ++i) {
      batch.token[i] = prompt[from + i]; batch.pos[i] = static_cast<llama_pos>(from + i); batch.n_seq_id[i] = 1; batch.seq_id[i][0] = 0; batch.logits[i] = (i + 1 == count_in_range);
    }
    const int32_t status = llama_decode(impl_->context, batch);
    llama_batch_free(batch);
    return status;
  };
  const size_t split = (boundary > reused && boundary < prompt.size()) ? boundary : prompt.size();
  int32_t prefill_status = decode_range(reused, split);
  if (prefill_status == 0 && split < prompt.size()) {
    // The context now holds exactly prompt[0, split): save it for the next turn.
    constexpr size_t kMaxSnapshotBytes = static_cast<size_t>(1536) << 20;
    try {
      const size_t bytes = llama_state_seq_get_size(impl_->context, 0);
      if (bytes > 0 && bytes <= kMaxSnapshotBytes) {
        impl_->snapshot_blob.resize(bytes);
        if (llama_state_seq_get_data(impl_->context, impl_->snapshot_blob.data(), impl_->snapshot_blob.size(), 0) == bytes) {
          impl_->snapshot_tokens.assign(prompt.begin(), prompt.begin() + static_cast<std::ptrdiff_t>(split));
          impl_->snapshot_key = request.cache_key;
          impl_->snapshot_valid = true;
          impl_->pub_snapshot_tokens = split; impl_->pub_snapshot_bytes = bytes;
        } else impl_->drop_snapshot();
      } else impl_->drop_snapshot();
    } catch (...) { impl_->drop_snapshot(); }
    prefill_status = decode_range(split, prompt.size());
  }
  if (prefill_status != 0) {
    impl_->state_tokens.clear(); impl_->publish();
    // llama_decode returns 2 when the abort callback fired. A client that gave
    // up is a cancellation, not an engine fault; the half-filled context is
    // discarded because `state_valid` is already false.
    if (prefill_status == 2 && cancellation->load()) {
      GenerationResult cancelled;
      cancelled.finish_reason = "cancelled"; cancelled.prompt_tokens = static_cast<unsigned>(prompt.size());
      return cancelled;
    }
    throw std::runtime_error("llama prefill failed");
  }
  impl_->state_tokens.assign(prompt.begin(), prompt.end());
  impl_->state_key = request.cache_key;
  impl_->state_valid = true;
  impl_->publish();
  GenerationResult result;
  result.prompt_tokens = static_cast<unsigned>(prompt.size());
  result.reused_prefix_tokens = static_cast<unsigned>(reused);
  if (impl_->spec_max > 0) {
    // Verify drafted tokens in one forward pass. A drafted token is kept only
    // when the model's own greedy choice at that position equals it, so what is
    // emitted is what plain greedy decoding emits (up to float rounding when
    // the batch size differs, which is why this is opt-in and measured).
    // `held` is the number of tokens in the context; `tok` was sampled but is
    // not in it yet.
    auto* memory = llama_get_memory(impl_->context);
    std::vector<llama_token> history(prompt.begin(), prompt.end());
    size_t held = prompt.size();
    llama_token tok = llama_sampler_sample(impl_->sampler, impl_->context, -1);
    // Anything that leaves the context holding unverified drafts must drop the
    // retained state, or the next request would reuse tokens that were never
    // confirmed.
    const auto abandon = [&] { impl_->state_valid = false; impl_->state_tokens.clear(); impl_->publish(); };
    while (true) {
      if (cancellation->load()) { result.finish_reason = "cancelled"; return result; }
      if (llama_vocab_is_eog(vocab, tok)) { result.finish_reason = "stop"; return result; }
      const auto emit = [&](llama_token t) {
        char piece[1024]; const int32_t size = llama_token_to_piece(vocab, t, piece, sizeof(piece), 0, false);
        if (size < 0 || !sink(std::string(piece, static_cast<size_t>(size)))) return false;
        ++result.generated_tokens; history.push_back(t); return true;
      };
      if (!emit(tok)) { result.finish_reason = "cancelled"; return result; }
      if (result.generated_tokens >= request.max_tokens) { result.finish_reason = "length"; return result; }

      const size_t room = request.max_tokens - result.generated_tokens;
      const std::vector<int32_t> draft = ngram_draft(history, 3, std::min<size_t>(impl_->spec_max, room));
      llama_batch step = llama_batch_init(static_cast<int32_t>(1 + draft.size()), 0, 1);
      step.n_tokens = static_cast<int32_t>(1 + draft.size());
      for (size_t i = 0; i < static_cast<size_t>(step.n_tokens); ++i) {
        step.token[i] = i == 0 ? tok : static_cast<llama_token>(draft[i - 1]);
        step.pos[i] = static_cast<llama_pos>(held + i); step.n_seq_id[i] = 1; step.seq_id[i][0] = 0; step.logits[i] = true;
      }
      const int32_t step_status = llama_decode(impl_->context, step);
      llama_batch_free(step);
      if (step_status != 0) {
        abandon();
        if (step_status == 2 && cancellation->load()) { result.finish_reason = "cancelled"; return result; }
        throw std::runtime_error("llama decode failed");
      }
      ++impl_->spec_steps; impl_->spec_drafted += draft.size();

      // From here the context holds `tok` plus UNVERIFIED drafts. Any exit,
      // including an exception from the sink or an allocation, must drop the
      // retained state, or the next request would trust tokens never confirmed.
      llama_token next = 0;
      size_t accepted = 0, keep = 0;
      try {
        // Position i's logits predict the token AFTER input i.
        next = llama_sampler_sample(impl_->sampler, impl_->context, 0);
        while (accepted < draft.size() && !llama_vocab_is_eog(vocab, next) && next == static_cast<llama_token>(draft[accepted])) {
          if (!emit(next)) { abandon(); result.finish_reason = "cancelled"; return result; }
          ++accepted;
          if (result.generated_tokens >= request.max_tokens) break;
          next = llama_sampler_sample(impl_->sampler, impl_->context, static_cast<int32_t>(accepted));
        }
        impl_->spec_accepted += accepted;
        keep = held + 1 + accepted;  // tok plus the drafts that held
        if (draft.size() > accepted && !llama_memory_seq_rm(memory, 0, static_cast<llama_pos>(keep), -1))
          throw std::runtime_error("llama speculative rollback failed");
        impl_->state_tokens.push_back(tok);
        for (size_t i = 0; i < accepted; ++i) impl_->state_tokens.push_back(static_cast<llama_token>(draft[i]));
      } catch (...) { abandon(); throw; }
      impl_->publish();
      held = keep;
      if (result.generated_tokens >= request.max_tokens) { result.finish_reason = "length"; return result; }
      tok = next;
    }
  }
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
    const int32_t decode_status = llama_decode(impl_->context, next);
    llama_batch_free(next);
    if (decode_status != 0) {
      impl_->state_valid = false; impl_->state_tokens.clear(); impl_->publish();
      if (decode_status == 2 && cancellation->load()) { result.finish_reason = "cancelled"; return result; }
      throw std::runtime_error("llama decode failed");
    }
    try { impl_->state_tokens.push_back(token); }  // now part of the retained context
    catch (...) { impl_->state_valid = false; impl_->state_tokens.clear(); impl_->publish(); throw; }
    impl_->publish();
  }
  result.finish_reason = "length";
  return result;
}

void LlamaBackend::reset() {
  impl_->clear_context();
  impl_->drop_snapshot();
}

void LlamaBackend::forget(const std::string& key) {
  // Only ever called with no generation in flight (see Engine::delete_session).
  if (!impl_->context || key.empty()) return;
  if (impl_->state_valid && impl_->state_key == key) impl_->clear_context();
  if (impl_->snapshot_valid && impl_->snapshot_key == key) impl_->drop_snapshot();
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
void LlamaBackend::forget(const std::string&) {}
void LlamaBackend::shutdown() {}
}  // namespace lae
#endif
