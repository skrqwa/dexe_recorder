// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/recorder_node.h"

namespace dexe_recorder {

FrameBuffer::FrameBuffer(size_t max_size) : max_size_(max_size) {}

void FrameBuffer::Push(Frame&& frame) {
  std::lock_guard<std::mutex> lk(mtx_);
  if (stopped_) {
    return;
  }
  if (queue_.size() >= max_size_) {
    queue_.pop_front();
    dropped_.fetch_add(1);
  }
  queue_.push_back(std::move(frame));
  cv_.notify_one();
}

bool FrameBuffer::Pop(Frame* out) {
  std::unique_lock<std::mutex> lk(mtx_);
  cv_.wait(lk, [this] { return stopped_ || !queue_.empty(); });
  if (stopped_ && queue_.empty()) {
    return false;
  }
  *out = std::move(queue_.front());
  queue_.pop_front();
  return true;
}

void FrameBuffer::Stop() {
  {
    std::lock_guard<std::mutex> lk(mtx_);
    stopped_ = true;
  }
  cv_.notify_all();
}

size_t FrameBuffer::size() const {
  std::lock_guard<std::mutex> lk(mtx_);
  return queue_.size();
}

}  // namespace dexe_recorder
