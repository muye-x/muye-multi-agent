"""阶段 4 Knowledge Worker 的受控写侧实现。

该包只由本地 CLI/Worker 使用；`muye-data` 始终只消费通过评测后生成的
`ResourceSnapshotV1`，不获得建库或写入 API。
"""
