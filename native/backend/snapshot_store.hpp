#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <list>
#include <string>
#include <utility>
#include <vector>

namespace lae {

// Saved copies of a conversation's sequence state (attention KV + recurrent),
// one per conversation key, most recently used first. The engine owns ONE live
// context; this is what lets a second conversation (a delegated job, another
// browser tab) borrow it without making the first one re-read its history:
// the first conversation's boundary snapshot is still here when it returns.
//
// Bounded twice: by count (`max_slots`) and by total bytes (`max_bytes`). The
// least recently used slot is evicted first. A blob larger than `max_bytes`
// on its own is not stored. No llama types here, so it is unit-testable.
struct SnapshotSlot {
  std::string key;
  std::vector<int32_t> tokens;  // what the saved state covers
  std::vector<uint8_t> blob;
};

class SnapshotStore {
 public:
  SnapshotStore(size_t max_slots = 4, size_t max_bytes = static_cast<size_t>(1) << 30)
      : max_slots_(max_slots), max_bytes_(max_bytes) {}

  size_t max_slots() const { return max_slots_; }
  size_t count() const { return slots_.size(); }
  size_t bytes() const { return bytes_; }
  // Tokens covered by the most recently used slot (0 when empty).
  size_t newest_tokens() const { return slots_.empty() ? 0 : slots_.front().tokens.size(); }

  // The slot for `key`, marked most recently used, or nullptr.
  const SnapshotSlot* find(const std::string& key) {
    if (key.empty()) return nullptr;
    for (auto it = slots_.begin(); it != slots_.end(); ++it) {
      if (it->key == key) { if (it != slots_.begin()) slots_.splice(slots_.begin(), slots_, it); return &slots_.front(); }
    }
    return nullptr;
  }

  // Stores `slot` as the most recently used, replacing any slot with the same
  // key, then evicts from the least recently used end. Returns whether it was kept.
  bool put(SnapshotSlot slot) {
    drop(slot.key);
    if (max_slots_ == 0 || slot.key.empty() || slot.blob.empty() || slot.blob.size() > max_bytes_) return false;
    bytes_ += slot.blob.size();
    slots_.push_front(std::move(slot));
    while (slots_.size() > max_slots_ || bytes_ > max_bytes_) { bytes_ -= slots_.back().blob.size(); slots_.pop_back(); }
    return true;
  }

  void drop(const std::string& key) {
    for (auto it = slots_.begin(); it != slots_.end(); ++it)
      if (it->key == key) { bytes_ -= it->blob.size(); slots_.erase(it); return; }
  }
  void clear() { slots_.clear(); bytes_ = 0; }

 private:
  size_t max_slots_;
  size_t max_bytes_;
  size_t bytes_ = 0;
  std::list<SnapshotSlot> slots_;
};

}  // namespace lae
