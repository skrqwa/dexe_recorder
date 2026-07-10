#!/usr/bin/env python3
"""模拟真实录制订阅者：完整接收 Image/CompressedImage 消息并占用带宽。

用法:
  python3 fake_subscriber.py <topic> <msg_type> [duration]
  msg_type: image | compressed
示例:
  python3 fake_subscriber.py /camera/left_eye_resize image 15
  python3 fake_subscriber.py /camera/kfc_compressed compressed 15
"""
import sys
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, CompressedImage


class FakeSubscriber(Node):
    def __init__(self, topic, msg_type):
        super().__init__('fake_sub_' + topic.replace('/', '_'))
        self.count = 0
        if msg_type == 'image':
            self.sub = self.create_subscription(Image, topic, self.cb, 10)
        else:
            self.sub = self.create_subscription(CompressedImage, topic, self.cb, 10)
        self.get_logger().info(f'subscribing {topic} ({msg_type})')

    def cb(self, msg):
        self.count += 1
        # 模拟真实处理：访问 data 字段确保完整反序列化
        _ = len(msg.data)


def main():
    topic = sys.argv[1]
    msg_type = sys.argv[2]
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 15

    rclpy.init()
    node = FakeSubscriber(topic, msg_type)
    import time
    end = time.time() + duration
    while time.time() < end and rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.1)
    node.get_logger().info(f'received {node.count} messages in {duration}s')
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
