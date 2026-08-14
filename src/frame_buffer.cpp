// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------

#include "dexe_recorder/recorder_node.h"

namespace dexe_recorder
{

/**
 * @brief 构造函数
 * @param max_size 队列最大容量（默认 600 帧）
 * @return 构造完成的帧缓冲
 *
 * @throws 不抛出异常

 */
FrameBuffer::FrameBuffer(size_t max_size) : max_size_(max_size) {}

/**
 * @brief 生产端：非阻塞推入帧
 *
 * 加锁后检查队列是否已满，满了则丢弃最旧帧并计数。
 * 推入后通知一个等待的消费者。
 *
 * @param frame 要推入的帧（移动语义）
 * @return 无
 * @throws std::bad_alloc 队列扩容失败
 */
void FrameBuffer::Push(Frame&& frame)
{
    std::lock_guard<std::mutex> lk(mtx_);
    if (stopped_)
    {
        return;
    }
    if (queue_.size() >= max_size_)
    {
        queue_.pop_front();
        dropped_.fetch_add(1);
    }
    queue_.push_back(std::move(frame));
    cv_.notify_one();
}

/**
 * @brief 消费端：阻塞等待并取出一帧
 *
 * 加锁后在条件变量上等待，直到队列非空或收到停止信号。
 * 停止且队列空时返回 false。
 *
 * @param out 输出帧
 * @return true 取到帧；false 队列已停止且为空
 * @throws 不抛出异常
 */
bool FrameBuffer::Pop(Frame* out)
{
    std::unique_lock<std::mutex> lk(mtx_);
    cv_.wait(lk, [this] { return stopped_ || !queue_.empty(); });
    if (stopped_ && queue_.empty())
    {
        return false;
    }
    *out = std::move(queue_.front());
    queue_.pop_front();
    return true;
}

/**
 * @brief 停止队列，唤醒所有等待的消费者
 *
 * 设置停止标志后通知所有在 Pop 中等待的线程，
 * 使它们立即返回 false（队列已停止且为空）。
 * @return 无
 *
 * @throws 不抛出异常
 */
void FrameBuffer::Stop()
{
    {
        std::lock_guard<std::mutex> lk(mtx_);
        stopped_ = true;
    }
    cv_.notify_all();
}

/**
 * @brief 获取累计丢弃帧数
 * @return 被丢弃的帧总数
 * @throws 不抛出异常
 */
size_t FrameBuffer::dropped_count() const
{
    return dropped_.load();
}

/**
 * @brief 获取当前队列长度
 * @return 队列中帧的数量
 * @throws 不抛出异常
 */
size_t FrameBuffer::size() const
{
    std::lock_guard<std::mutex> lk(mtx_);
    return queue_.size();
}

}  // namespace dexe_recorder
