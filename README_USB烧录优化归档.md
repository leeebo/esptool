# ESP32-C6 USB 烧录通信优化 — 本地归档总索引

> **项目**：基于 `E:\esptool-libo\esptool`（esptool v5.3.0 fork）优化 USB 烧录在**通信质量差**场景下的处理：异常时多次重发命令，提升烧录成功率。  
> **现状**：成功率已明显提高（曾完整写入 4MB 固件），但**仍有一定概率** USB 烧录失败，需软硬件继续优化。  
> **归档日期**：2026-07-01

---

## 一、归档文件清单

| 文件 | 内容 |
|------|------|
| **[README_USB烧录优化归档.md](README_USB烧录优化归档.md)** | 本文件：总索引、时间线、优化方向 |
| **[分析与修复记录_USB烧录与不稳定链路.md](分析与修复记录_USB烧录与不稳定链路.md)** | 技术分析 + 配置 + 代码摘要 + **附录 A–F 完整报错日志** |
| **[分析与修复记录_原始日志全集.txt](分析与修复记录_原始日志全集.txt)** | 用户提交的 6 份原始 trace/报错（纯文本，便于搜索） |
| **[交互记录_完整对话摘录.md](交互记录_完整对话摘录.md)** | 沟通全过程：用户提问与助手分析回复（39 轮，日志处指向附录） |
| **[交互记录_原始transcript.jsonl](交互记录_原始transcript.jsonl)** | Cursor 对话原始 JSONL（含完整结构，可程序解析） |
| **[代码修改说明.md](代码修改说明.md)** | 各文件改动目的、机制与调用关系 |
| **[代码修改_diff.patch](代码修改_diff.patch)** | `git diff` 补丁（8 个源码文件） |
| **[esptool.cfg](esptool.cfg)** | 本机推荐运行时配置 |

---

## 二、问题与根因（结论）

### 现象

- `Serial data stream stopped: Possible serial noise or corruption`
- 主机发命令 N，读到命令 N-1 的 SLIP 回包（opcode 错位：`0x08`/`0x14`/`0x09`/`0x0a` 等）
- 失败可出现在：连接、`disable_watchdogs`、`SPI_ATTACH`、Flash 探测、`SPI_SET_PARAMS`、写后 MD5

### 根因

**USB-Serial/JTAG RX 积压 / 乱序 / 丢包**，不是 efuse 检查或芯片型号识别错误。  
`GET_SECURITY_INFO (0x14)` 是只读安全信息探测，**不是**烧 efuse。

### 硬件背景

自研 ESP32-C6 板 USB 走线/阻抗/接地欠佳，ROM 一次 SYNC 可回 8 个重复包，说明链路边际。

---

## 三、沟通与排查时间线

| 阶段 | 用户动作 / 日志 | 分析结论 | 采取的措施 |
|------|-----------------|----------|------------|
| 1 | 提问：已有 `lost_response_resends`，仍偶发失败，如何优化 | 原机制只覆盖「响应完全丢失」；多数命令未 `allow_resend`；陈旧 opcode 会空等 3s 超时 | 梳理机制边界，规划扩展 |
| 2 | 附录 A：官方 esptool，COM12，`--trace` | 失败在 `disable_watchdogs` 第一条 `WRITE_REG`；SYNC 后 RX 积压 8 个回包 | `_drain_stale_input()`；`disable_watchdogs` flush + `allow_resend` |
| 3 | 附录 B：fork COM23 | `GET_SECURITY_INFO` 重发成功；`disable_watchdogs` 因残留包失败 | 陈旧 opcode **立即** flush+重发（不等满超时） |
| 4 | 用户问能否跳过 efuse | 澄清非 efuse；失败仍是 RX 错位 | `skip_security_info_check`、`skip_watchdog_disable` + `esptool.cfg` |
| 5 | 附录 C：加载 cfg | **连接成功**；`SPI_ATTACH` 失败 | `transport_check_command()`；flaky USB 时 `check_command` 自动加固 |
| 6 | 附录 D/E：长 trace | XMC、`SPI_SET_PARAMS` 仍偶发 stream stopped | `skip_flash_verify`；增大 `write_block_attempts` |
| 7 | 附录 F：`-p COM23` 无 trace | **`Wrote 4194304 bytes` 成功**；ROM 不支持 `0x13` MD5 报错退出 | `write_flash` 捕获 `UnsupportedCommandError` 跳过 MD5 |
| 8 | 用户要求日志写入分析记录 | — | 生成附录 A–F 与 `原始日志全集.txt` |
| 9 | 用户要求保存全部交互与分析 | — | 本归档目录 |

---

## 四、已实施的软件修复（摘要）

详见 [代码修改说明.md](代码修改说明.md)。

1. **`lost_response_resends`**：空响应与**陈旧 opcode** 时重发（`allow_resend=True`）
2. **`_drain_stale_input()`**：`sync()` 后清空多余 SYNC 回包
3. **`transport_check_command()`**：每次尝试前 `flush_input`，失败外层重试 `write_block_attempts` 次
4. **`_flaky_usb_transport()`**：USB-JTAG + `lost_response_resends>0` 时全局加固 `check_command` / `read_reg` / `write_reg`
5. **配置跳过**：`skip_security_info_check`、`skip_watchdog_disable`、`skip_flash_verify`
6. **MD5**：`--no-stub` 下 ROM 无 `SPI_FLASH_MD5` 时不因校验失败误报整次烧录失败

**统计**：8 个文件约 +235 / -142 行（含删除旧 `UPDATE_lost_response_resends.md`）。

---

## 五、推荐烧录命令

```powershell
cd E:\esptool-libo\esptool
pip install -e .
python -m esptool -p COM23 --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size 4MB 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
```

**务必** `-p COM23`，避免扫描 COM1 等无关口。

---

## 六、仍可能失败的原因与进一步优化

### 6.1 软件（可在本 fork 继续）

| 方向 | 说明 |
|------|------|
| 提高重试上限 | `esptool.cfg` 中 `lost_response_resends`、`write_block_attempts`、`connect_attempts` 可再加大（代价：单次烧录更慢） |
| 写 Flash 块级重试 | `FLASH_BEGIN`/`FLASH_DATA` 路径对 `SerialReaderStoppedError` 做块级重发（需谨慎，非完全幂等） |
| 降低波特率 / USB 缓冲 | 尝试更低 `baud` 或 OS 层 USB 串口驱动参数（若走 UART 桥） |
| 写前长 drain | 增大 `_drain_stale_input` 的 `idle_timeout` 或命令间固定 `sleep` |
| 失败自动整流程重试 | 脚本层：连接失败则 `usb-reset` 后从头重试 N 次 |
| 生产用 stub | 链路稳定后可去掉 `--no-stub`，stub 协议通常更稳（需先能稳定上传 stub） |

### 6.2 硬件（根本手段）

- 缩短 USB D+/D- 走线，90Ω 差分阻抗，完整地参考面
- 靠近连接器加 ESD/TVS；避免 USB 与高频/大电流共地噪声
- 供电与 BOOT/EN 时序稳定；劣质线材/Hub 会显著恶化
- 量产考虑外置 USB-UART（CH340/CP2102）代替片上 USB-JTAG 做烧录口

### 6.3 流程

- 固定 COM 口、固定命令、固定 `esptool.cfg`
- 失败时保留 `--trace` 日志对照附录格式
- 写入成功后用串口/AT 命令验证启动，不依赖 esptool 退出码 alone

---

## 七、验证清单

- [ ] `pip install -e .` 后复烧，无 traceback 正常退出
- [ ] 上电 AT 固件正常
- [ ] 连续烧录 10+ 次统计成功率
- [ ] 对比开/关 `skip_flash_verify` 的差异（仅调试）

---

## 八、相关对话

Cursor 会话 transcript ID：`9ed1b6ba-ab3e-4447-9e94-028ddd006ec2`
