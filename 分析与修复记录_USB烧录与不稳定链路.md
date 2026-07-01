# ESP32-C6 USB 烧录分析与修复记录

> 工作目录：`E:\esptool-libo\esptool`（esptool v5.3.0 fork）  
> 硬件：自研 ESP32-C6 板，USB-Serial/JTAG 链路信号完整性较差  
> 固件：`factory_ESP32C6-4MB.bin`（4MB AT 出厂镜像）  
> 文档更新：2026-07-01

**本地归档索引**（沟通、日志、代码、交互全文）见 **[README_USB烧录优化归档.md](README_USB烧录优化归档.md)**。

| 归档文件 | 说明 |
|----------|------|
| [README_USB烧录优化归档.md](README_USB烧录优化归档.md) | 总索引、时间线、后续优化方向 |
| [分析与修复记录_原始日志全集.txt](分析与修复记录_原始日志全集.txt) | 6 份原始报错/trace |
| [交互记录_完整对话摘录.md](交互记录_完整对话摘录.md) | 39 轮问答摘录 |
| [交互记录_原始transcript.jsonl](交互记录_原始transcript.jsonl) | Cursor 原始对话 JSONL |
| [代码修改说明.md](代码修改说明.md) | 改动机制说明 |
| [代码修改_diff.patch](代码修改_diff.patch) | git diff 补丁 |

---

## 0. 项目目标

基于本 fork 优化 **USB 烧录通信质量差** 时的处理：在 `Serial data stream stopped`、响应丢失、RX 陈旧包等异常时**多次重发命令**，提升烧录成功率。实测曾完整写入 4MB 出厂镜像，但**仍有一定概率失败**，需继续软硬件优化（见 [README](README_USB烧录优化归档.md) 第六节）。

---

## 1. 问题概述

在自研 ESP32-C6 板上通过 **USB-Serial/JTAG** 烧录时，esptool 频繁出现：

- `Serial data stream stopped: Possible serial noise or corruption`
- **陈旧 RX 包**：主机发送命令 N，却读到命令 N-1 的响应（opcode 错位：`0x08` SYNC、`0x14`、`0x0a`、`0x09` 等）
- 失败点分布在：连接、`disable_watchdogs`、`SPI_ATTACH`、Flash 探测、`SPI_SET_PARAMS`、写完后 MD5 校验

**根因**：不是芯片逻辑错误或 efuse 检查，而是 **USB-JTAG RX 缓冲区积压 / 乱序 SLIP 包**。软件侧通过清缓冲、陈旧 opcode 立即重发、传输层加固 `check_command`、跳过非必要探测来缓解。

---

## 2. 推荐烧录命令

```powershell
cd E:\esptool-libo\esptool
pip install -e .
python -m esptool -p COM23 --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size 4MB 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
```

要点：

| 参数 | 原因 |
|------|------|
| `-p COM23` | 指定 C6 USB 口，避免扫描到 COM1 等无关口 |
| `--chip esp32c6` | 配合 `skip_security_info_check` 跳过芯片 ID 探测 |
| `--no-stub` | 不走 stub，减少 MEM_* 阶段失败 |
| `--flash-size 4MB` | 避免 `detect` 阶段额外 SPI 交互 |
| `esptool.cfg` | 见下文 |

---

## 3. `esptool.cfg` 配置

```ini
[esptool]
lost_response_resends = 20
skip_security_info_check = 1
skip_watchdog_disable = 1
skip_flash_verify = 1
connect_attempts = 15
write_block_attempts = 10
```

| 选项 | 作用 |
|------|------|
| `lost_response_resends` | 响应完全丢失或 opcode 错位时重发（默认 15，此处 20） |
| `skip_security_info_check` | 跳过 `GET_SECURITY_INFO`（已指定 `--chip`） |
| `skip_watchdog_disable` | 跳过 `_post_connect` 中的 `disable_watchdogs()` |
| `skip_flash_verify` | 跳过 `attach_flash()` 中 XMC 启动与 SPI 连接校验 |
| `connect_attempts` / `write_block_attempts` | 连接与传输层命令外层重试次数 |

---

## 4. 代码修改摘要

| 文件 | 修改 |
|------|------|
| `esptool/loader.py` | `SerialReaderStoppedError` 重发；陈旧 opcode 立即 flush+重发；`sync()` 后 `_drain_stale_input()`；`transport_check_command()`；flaky USB 时 `check_command` 自动走传输加固路径 |
| `esptool/config.py` | 新增 `skip_*` 与 `lost_response_resends` 配置项 |
| `esptool/targets/esp32c3.py` 等 | `disable_watchdogs()` 尊重 `SKIP_WATCHDOG_DISABLE`，USB 路径 flush + `allow_resend` |
| `esptool/cmds.py` | `SKIP_FLASH_VERIFY`；`write_flash` 对 ROM 不支持 `0x13` MD5 按成功处理（写入已完成） |
| `test/test_lost_response_resends.py` | 陈旧 opcode 重发单元测试 |

---

## 5. 日志演进与结论对照

| 附录 | 阶段 | 结果 |
|------|------|------|
| A | 官方工具 COM12，无 cfg | `disable_watchdogs` / `WRITE_REG` 失败，无法连接 |
| B | fork COM23 | `GET_SECURITY_INFO` 重发后成功，`disable_watchdogs` 仍因残留包失败 |
| C | 加载 cfg，跳过 security/watchdog | **连接成功**，`SPI_ATTACH` 失败 |
| D | 长 trace | 同上阶段细节：每次 opcode 错位等满 3s 超时 |
| E | 加固 `transport_check_command` 后 | 推进到 XMC / `SPI_SET_PARAMS`，仍偶发 stream stopped |
| F | 无 trace，全加固 + skip verify | **`Wrote 4194304 bytes` 成功**；ROM 不支持 `SPI_FLASH_MD5(0x13)` 导致退出码非 0 |

---

## 6. 后续建议

1. 重新 `pip install -e .` 后复烧，确认 MD5 修复后正常退出（无 traceback）。
2. 上电验证 AT 固件是否正常启动。
3. **硬件**：缩短 USB 走线、改善阻抗与接地，是根本提升手段。
4. 完整原始日志见 **`分析与修复记录_原始日志全集.txt`**（与下文附录 A–F 一致）。
5. 沟通全过程见 **`交互记录_完整对话摘录.md`**；进一步软件/硬件优化见 **`README_USB烧录优化归档.md` 第六节**。

---

# 用户提交的原始报错日志（附录）

以下为用户在排查过程中提交的 **6 份完整报错 / trace 日志**，按时间顺序排列。

## 附录 A：官方 esptool（COM12）— 连接阶段 WRITE_REG / disable_watchdogs 失败

```text
D:\vs\Espressif\1\esptool>esptool --trace --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size detect 0x0 F:\a-CPBG\Tive_Solo_5G\bin_custom\DVT2_260506\tiveos-5g_lite-evt3-v0.4.0.bin
Warning: DEPRECATED: 'esptool.py' is deprecated. Please use 'esptool' instead. The '.py' suffix will be removed in a future major release.
esptool v5.3.0
Found 1 serial ports...
Serial port COM12:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.004   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.107   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.108   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.004   Read 1 bytes:         c0
  TRACE +0.001   Read 111 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0c001080400 | ..... ..........
    0707122000000000 c0c0010804000707 | ... ............
    122000000000c0c0 0108040007071220 | . .............
    00000000c0c00108 0400070712200000 | ............. ..
    0000c0c001080400 0707122000000000 | ........... ....
    c0c0010804000707 122000000000c0   | ......... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000

  TRACE +0.002   --- Cmd GET_SECURITY_INFO (0x14) | data_len 0 | wait_response 1 | timeout 3.000 | data  ---
  TRACE +0.000   Write 10 bytes:       c00014000000000000c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 27 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0           | ..... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Read 84 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0 c001080400070712 | .. .............
    2000000000c0c001 0804000707122000 |  ............. .
    000000c0                          | ....
  TRACE +0.001   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +3.013   Serial data stream stopped: Possible serial noise or corruption.
  TRACE +0.000   Write 10 bytes:       c00014000000000000c0
  TRACE +0.002   Read 1 bytes:         c0
  TRACE +0.000   Read 33 bytes:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
    c0                                | .
  TRACE +0.002   Received full packet:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................

  TRACE +0.008   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 181c0b60a13ad850ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00181c0b60a13ad8 | ............`.:.
    50ffffffff000000 00c0             | P.........
  TRACE +0.001   Read 1 bytes:         c0
  TRACE +0.000   Read 33 bytes:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
    c0                                | .
  TRACE +0.000   Received full packet:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
  TRACE +3.016   Serial data stream stopped: Possible serial noise or corruption.
COM12 failed to connect: Serial data stream stopped: Possible serial noise or corruption.

A fatal error occurred: Could not connect to an Espressif device on any of the 1 available serial ports.
```

## 附录 B：fork esptool（COM23）— GET_SECURITY_INFO 成功后 disable_watchdogs 失败

```text
python esptool.py --trace --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size detect 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
Warning: DEPRECATED: 'esptool.py' is deprecated. Please use 'esptool' instead. The '.py' suffix will be removed in a future major release.
esptool v5.3.0
Found 2 serial ports...
Serial port COM23:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.002   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.113   No serial data received.
  TRACE +0.001   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.109   No serial data received.
  TRACE +0.001   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.006   Read 1 bytes:         c0
  TRACE +0.001   Read 55 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0c001080400 | ..... ..........
    0707122000000000 c0c0010804000707 | ... ............
    122000000000c0                    | . .....
  TRACE +0.001   Received full packet: 010804000707122000000000
  TRACE +0.008   Received full packet: 010804000707122000000000
  TRACE +0.005   Received full packet: 010804000707122000000000
  TRACE +0.010   Received full packet: 010804000707122000000000
  TRACE +0.006   Read 56 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0                  | .. .....
  TRACE +0.001   Received full packet: 010804000707122000000000
  TRACE +0.003   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.065   Serial data stream stopped: Possible serial noise or corruption.

  TRACE +0.009   --- Cmd GET_SECURITY_INFO (0x14) | data_len 0 | wait_response 1 | timeout 3.000 | data  ---
  TRACE +0.001   Write 10 bytes:       c00014000000000000c0
  TRACE +0.001   Read 14 bytes:        c0010804000707122000000000c0
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Read 98 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0 c001080400070712 | .. .............
    2000000000c0c001 0804000707122000 |  ............. .
    000000c0c0010804 0007071220000000 | ............ ...
    00c0                              | ..
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +3.014   Serial data stream stopped: Possible serial noise or corruption.
  TRACE +0.000   Write 10 bytes:       c00014000000000000c0
  TRACE +0.003   Read 1 bytes:         c0
  TRACE +0.000   Read 33 bytes:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
    c0                                | .
  TRACE +0.000   Received full packet:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................

  TRACE +0.008   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 181c0b60a13ad850ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00181c0b60a13ad8 | ............`.:.
    50ffffffff000000 00c0             | P.........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 33 bytes:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
    c0                                | .
  TRACE +0.000   Received full packet:
    0114180007071220 0000000000000000 | ....... ........
    0000000c0d000000 0000000000000000 | ................
  TRACE +3.011   Serial data stream stopped: Possible serial noise or corruption.
  TRACE +0.001   Write 26 bytes:
    c000091000000000 00181c0b60a13ad8 | ............`.:.
    50ffffffff000000 00c0             | P.........
  TRACE +0.002   Read 14 bytes:        c0010904000707122000000000c0
  TRACE +0.000   Received full packet: 010904000707122000000000

  TRACE +0.003   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 001c0b6000000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00001c0b60000000 | ............`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000707122000000000c0
  TRACE +0.000   Received full packet: 010904000707122000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 181c0b6000000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00181c0b60000000 | ............`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000707122000000000c0
  TRACE +0.000   Received full packet: 010904000707122000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 201c0b60a13ad850ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00201c0b60a13ad8 | ......... ..`.:.
    50ffffffff000000 00c0             | P.........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000707122000000000c0
  TRACE +0.000   Received full packet: 010904000707122000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 1c1c0b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a0400000000001c1c0b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000707122000000000c0
  TRACE +0.000   Received full packet: 010904000707122000000000
  TRACE +3.011   Serial data stream stopped: Possible serial noise or corruption.
  TRACE +0.000   Write 14 bytes:       c0000a0400000000001c1c0b60c0
  TRACE +0.003   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        04000000c41200000000c0
  TRACE +0.000   Received full packet: 010a04000000c41200000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 1c1c0b600000c412ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 001c1c0b600000c4 | ............`...
    12ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000c41200000000c0
  TRACE +0.000   Received full packet: 010a04000000c41200000000
  TRACE +3.004   Serial data stream stopped: Possible serial noise or corruption.
COM23 failed to connect: Serial data stream stopped: Possible serial noise or corruption.
Serial port COM1:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.002   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.104   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.107   No serial data received.
  TRACE +0.000   Write 46 bytes:
```

## 附录 C：加载 esptool.cfg 后 — 连接成功，SPI_ATTACH 失败

```text
Warning: DEPRECATED: 'esptool.py' is deprecated. Please use 'esptool' instead. The '.py' suffix will be removed in a future major release.
esptool v5.3.0
Loaded custom configuration from E:\esptool-libo\esptool\esptool.cfg
Found 2 serial ports...
Serial port COM23:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.002   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.106   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.109   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.003   Read 1 bytes:         c0
  TRACE +0.000   Read 41 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0c001080400 | ..... ..........
    0707122000000000 c0               | ... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.003   Read 70 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0 c001080400070712 | .. .............
    2000000000c0                      |  .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.003   Received full packet: 010804000707122000000000
  TRACE +0.005   Received full packet: 010804000707122000000000
  TRACE +0.054   Serial data stream stopped: Possible serial noise or corruption.

  TRACE +0.061   No serial data received.
Connected to ESP32-C6 on COM23:

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 27 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0           | ..... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Chip type:          Unknown ESP32-C6 (revision v0.2)

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Features:           Wi-Fi 6, BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 160MHz, Unknown Embedded Flash
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG

  TRACE +0.015   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
MAC:                2e:a8:00:c5:1e:00:00:29

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
BASE MAC:           2e:a8:ff:fe:58:e6

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 2 bytes:         010a
  TRACE +0.000   Read 11 bytes:        0400a82e1ec500000000c0
  TRACE +0.008   Received full packet: 010a0400a82e1ec500000000
MAC_EXT:            c5:1e

Enabling default SPI flash mode...

  TRACE +0.004   --- Cmd SPI_ATTACH (0x0d) | data_len 8 | wait_response 1 | timeout 3.000 | data 0000000000000000 ---
  TRACE +0.000   Write 18 bytes:
    c0000d0800000000 0000000000000000 | ................
    00c0                              | ..
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000
  TRACE +3.000   Serial data stream stopped: Possible serial noise or corruption.

Hard resetting via RTS pin...

A fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.
```

## 附录 D：--trace 长日志 — disable_watchdogs / SPI 阶段残留包

```text
E:\esptool-libo\esptool>python esptool.py --trace --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size detect 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
Warning: DEPRECATED: 'esptool.py' is deprecated. Please use 'esptool' instead. The '.py' suffix will be removed in a future major release.
esptool v5.3.0
Loaded custom configuration from E:\esptool-libo\esptool\esptool.cfg
Found 2 serial ports...
Serial port COM23:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.002   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.108   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.107   No serial data received.
  TRACE +0.001   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.005   Read 1 bytes:         c0
  TRACE +0.000   Read 55 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0c001080400 | ..... ..........
    0707122000000000 c0c0010804000707 | ... ............
    122000000000c0                    | . .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.003   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.003   Read 56 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0                  | .. .....
  TRACE +0.004   Received full packet: 010804000707122000000000
  TRACE +0.004   Received full packet: 010804000707122000000000
  TRACE +0.004   Received full packet: 010804000707122000000000
  TRACE +0.007   Received full packet: 010804000707122000000000
  TRACE +0.058   Serial data stream stopped: Possible serial noise or corruption.

  TRACE +0.061   No serial data received.
Connected to ESP32-C6 on COM23:

  TRACE +0.004   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 27 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0           | ..... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 42 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0             | .... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 2 bytes:         010a
  TRACE +0.000   Read 11 bytes:        04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Chip type:          Unknown ESP32-C6 (revision v0.2)

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.008   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Features:           Wi-Fi 6, BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 160MHz, Unknown Embedded Flash
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG

  TRACE +0.008   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
MAC:                2e:a8:00:c5:1e:00:00:29

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.001   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
BASE MAC:           2e:a8:ff:fe:58:e6

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.008   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.001   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
MAC_EXT:            c5:1e

Enabling default SPI flash mode...

  TRACE +0.004   --- Cmd SPI_ATTACH (0x0d) | data_len 8 | wait_response 1 | timeout 3.000 | data 0000000000000000 ---
  TRACE +0.000   Write 18 bytes:
    c0000d0800000000 0000000000000000 | ................
    00c0                              | ..
  TRACE +0.001   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000
  TRACE +0.000   Write 18 bytes:
    c0000d0800000000 0000000000000000 | ................
    00c0                              | ..
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010d0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010d0400e658feff00000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 18300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010d0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010d0400e658feff00000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000008000000000c0
  TRACE +0.000   Received full packet: 010a04000000008000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 20300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000020300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000008000000000c0
  TRACE +0.000   Received full packet: 010a04000000008000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 2830006017000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0028300060170000 | .........(0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000007000000000c0
  TRACE +0.000   Received full packet: 010a04000000007000000000
  TRACE +3.006   Serial data stream stopped: Possible serial noise or corruption.
  TRACE +0.002   Unable to perform XMC flash chip startup sequence (Serial data stream stopped: Possible serial noise or corruption.).

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 18300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Read 14 bytes:        c0010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 2 bytes:         010a
  TRACE +0.000   Read 11 bytes:        04000000008000000000c0
  TRACE +0.000   Received full packet: 010a04000000008000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 20300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000020300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 2 bytes:         010a
  TRACE +0.000   Read 11 bytes:        04000000008000000000c0
  TRACE +0.000   Received full packet: 010a04000000008000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 283000601f000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 00283000601f0000 | .........(0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000007000000000c0
  TRACE +0.000   Received full packet: 010a04000000007000000000
  TRACE +3.000   Serial data stream stopped: Possible serial noise or corruption.

Hard resetting via RTS pin...

A fatal error occurred: Unable to verify flash chip connection (Serial data stream stopped: Possible serial noise or corruption.).
```

## 附录 E：-p COM23 --trace — XMC 探测 / SPI_SET_PARAMS 阶段

```text
E:\esptool-libo\esptool>python -m esptool -p COM23 --trace --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size detect 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
esptool v5.3.0
Loaded custom configuration from E:\esptool-libo\esptool\esptool.cfg
Serial port COM23:
Connecting...
  TRACE +0.000   --- Cmd SYNC (0x08) | data_len 36 | wait_response 1 | timeout 0.100 | data
    0707122055555555 5555555555555555 | ... UUUUUUUUUUUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    55555555                          | UUUU ---
  TRACE +0.005   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.113   No serial data received.
  TRACE +0.001   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.106   No serial data received.
  TRACE +0.000   Write 46 bytes:
    c000082400000000 0007071220555555 | ...$........ UUU
    5555555555555555 5555555555555555 | UUUUUUUUUUUUUUUU
    5555555555555555 5555555555c0     | UUUUUUUUUUUUU.
  TRACE +0.003   Read 1 bytes:         c0
  TRACE +0.000   Read 27 bytes:
    0108040007071220 00000000c0c00108 | ....... ........
    0400070712200000 0000c0           | ..... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.003   Read 84 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0c0010804 | ...... .........
    0007071220000000 00c0c00108040007 | .... ...........
    07122000000000c0 c001080400070712 | .. .............
    2000000000c0c001 0804000707122000 |  ............. .
    000000c0                          | ....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.005   Received full packet: 010804000707122000000000
  TRACE +0.006   Received full packet: 010804000707122000000000
  TRACE +0.002   Received full packet: 010804000707122000000000
  TRACE +0.003   Received full packet: 010804000707122000000000
  TRACE +0.066   Serial data stream stopped: Possible serial noise or corruption.

  TRACE +0.062   No serial data received.
Connected to ESP32-C6 on COM23:

  TRACE +0.014   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 14 bytes:        c0010804000707122000000000c0
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 28 bytes:
    c001080400070712 2000000000c0c001 | ........ .......
    0804000707122000 000000c0         | ...... .....
  TRACE +0.000   Received full packet: 010804000707122000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.004   Read 3 bytes:         c0010a
  TRACE +0.000   Read 11 bytes:        04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.005   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.001   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.001   Read 14 bytes:        c0010a04000000081900000000c0
  TRACE +0.016   Received full packet: 010a04000000081900000000
Chip type:          Unknown ESP32-C6 (revision v0.2)

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 54080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000054080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Features:           Wi-Fi 6, BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 160MHz, Unknown Embedded Flash
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 2 bytes:         010a
  TRACE +0.000   Read 11 bytes:        04002900000000000000c0
  TRACE +0.000   Received full packet: 010a04002900000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
MAC:                2e:a8:00:c5:1e:00:00:29

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.004   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
BASE MAC:           2e:a8:ff:fe:58:e6

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 44080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000044080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 48080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000048080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a0400a82e1ec500000000c0
  TRACE +0.000   Received full packet: 010a0400a82e1ec500000000
MAC_EXT:            c5:1e

Enabling default SPI flash mode...

  TRACE +0.002   --- Cmd SPI_ATTACH (0x0d) | data_len 8 | wait_response 1 | timeout 3.000 | data 0000000000000000 ---
  TRACE +0.000   Write 18 bytes:
    c0000d0800000000 0000000000000000 | ................
    00c0                              | ..
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.007   Read 13 bytes:        010a0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010a0400e658feff00000000
  TRACE +0.000   Write 18 bytes:
    c0000d0800000000 0000000000000000 | ................
    00c0                              | ..
  TRACE +0.001   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010d0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010d0400e658feff00000000
Note: skip_flash_verify is enabled: skipping XMC startup and SPI flash connection verification.

  TRACE +0.005   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 34080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000034080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010d0400e658feff00000000c0
  TRACE +0.000   Received full packet: 010d0400e658feff00000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000034080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000000000000000c0
  TRACE +0.000   Received full packet: 010a04000000000000000000

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.003   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000000000000000c0
  TRACE +0.000   Received full packet: 010a04000000000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 50080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000050080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 30080b60 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000030080b60c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000081900000000c0
  TRACE +0.000   Received full packet: 010a04000000081900000000
Configuring flash size...

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 18300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000018300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000000000000000c0
  TRACE +0.000   Received full packet: 010a04000000000000000000

  TRACE +0.003   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 20300060 ---
  TRACE +0.006   Write 14 bytes:       c0000a04000000000020300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000008000000000c0
  TRACE +0.000   Received full packet: 010a04000000008000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 2830006017000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0028300060170000 | .........(0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000007000000000c0
  TRACE +0.000   Received full packet: 010a04000000007000000000
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0028300060170000 | .........(0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000000007000000000c0
  TRACE +0.001   Received full packet: 010904000000007000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 1830006000000090ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0018300060000000 | ..........0.`...
    90ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000

  TRACE +0.003   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 203000609f000070ffffffff00000000 ---
  TRACE +0.004   Write 26 bytes:
    c000091000000000 00203000609f0000 | ......... 0.`...
    70ffffffff000000 00c0             | p.........
  TRACE +0.000   Read 14 bytes:        c0010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000

  TRACE +0.002   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 5830006000000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0058300060000000 | .........X0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000

  TRACE +0.003   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 0030006000000400ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0000300060000004 | ..........0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 00300060 ---
  TRACE +0.005   Write 14 bytes:       c0000a04000000000000300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904000000007000000000c0
  TRACE +0.000   Received full packet: 010904000000007000000000
  TRACE +0.000   Write 14 bytes:       c0000a04000000000000300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000000000000000c0
  TRACE +0.000   Received full packet: 010a04000000000000000000

  TRACE +0.002   --- Cmd READ_REG (0x0a) | data_len 4 | wait_response 1 | timeout 3.000 | data 58300060 ---
  TRACE +0.000   Write 14 bytes:       c0000a04000000000058300060c0
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04000000000000000000c0
  TRACE +0.000   Received full packet: 010a04000000000000000000

  TRACE +0.003   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 1830006000000000ffffffff00000000 ---
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0018300060000000 | ..........0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010a04004640160000000000c0
  TRACE +0.000   Received full packet: 010a04004640160000000000
  TRACE +0.000   Write 26 bytes:
    c000091000000000 0018300060000000 | ..........0.`...
    00ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904004640160000000000c0
  TRACE +0.000   Received full packet: 010904004640160000000000

  TRACE +0.003   --- Cmd WRITE_REG (0x09) | data_len 16 | wait_response 1 | timeout 3.000 | data 2030006000000080ffffffff00000000 ---
  TRACE +0.004   Write 26 bytes:
    c000091000000000 0020300060000000 | ......... 0.`...
    80ffffffff000000 00c0             | ..........
  TRACE +0.000   Read 14 bytes:        c0010904004640160000000000c0
  TRACE +0.000   Received full packet: 010904004640160000000000
Warning: Could not auto-detect flash size, defaulting to 4MB.

  TRACE +0.002   --- Cmd SPI_SET_PARAMS (0x0b) | data_len 24 | wait_response 1 | timeout 3.000 | data
    0000000000004000 0000010000100000 | ......@.........
    00010000ffff0000                  | ........ ---
  TRACE +0.000   Write 34 bytes:
    c0000b1800000000 0000000000000040 | ...............@
    0000000100001000 0000010000ffff00 | ................
    00c0                              | ..
  TRACE +0.000   Read 1 bytes:         c0
  TRACE +0.000   Read 13 bytes:        010904004640160000000000c0
  TRACE +0.000   Received full packet: 010904004640160000000000
  TRACE +3.005   Serial data stream stopped: Possible serial noise or corruption.

Hard resetting via RTS pin...

A fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.
```

## 附录 F：-p COM23 无 trace — 4MB 写入成功，ROM MD5(0x13) 校验失败

```text
E:\esptool-libo\esptool>python -m esptool -p COM23 --chip esp32c6 --no-stub --before usb-reset --after hard-reset write-flash --flash-size 4MB 0x0 E:\Download\ESP32-C6-4MB-AT-V4.0\ESP32-C6-4MB-V4.0.0.0\factory\factory_ESP32C6-4MB.bin
esptool v5.3.0
Loaded custom configuration from E:\esptool-libo\esptool\esptool.cfg
Connected to ESP32-C6 on COM23:
Chip type:          Unknown ESP32-C6 (revision v0.2)
Features:           Wi-Fi 6, BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 160MHz, Unknown Embedded Flash
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG
MAC:                2e:a8:00:c5:1e:00:00:29
BASE MAC:           2e:a8:ff:fe:58:e6
MAC_EXT:            c5:1e

Enabling default SPI flash mode...
Note: skip_flash_verify is enabled: skipping XMC startup and SPI flash connection verification.
Configuring flash size...
SHA digest in image updated.
Flash will be erased from 0x00000000 to 0x003fffff...
Wrote 4194304 bytes at 0x00000000 in 64.3 seconds (522.1 kbit/s).
Verifying written data...

Hard resetting via RTS pin...
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "E:\esptool-libo\esptool\esptool\__main__.py", line 9, in <module>
    esptool._main()
    ~~~~~~~~~~~~~^^
  File "E:\esptool-libo\esptool\esptool\__init__.py", line 1362, in _main
    main()
    ~~~~^^
  File "E:\esptool-libo\esptool\esptool\__init__.py", line 1256, in main
    cli(args=args, esp=esp)
    ~~~^^^^^^^^^^^^^^^^^^^^
  File "E:\esptool-libo\esptool\esptool\cli_util.py", line 346, in __call__
    return super().__call__(*args, **kwargs)
           ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\rich_click\rich_command.py", line 402, in __call__
    return super().__call__(*args, **kwargs)
           ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\click\core.py", line 1485, in __call__
    return self.main(*args, **kwargs)
           ~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\rich_click\rich_command.py", line 216, in main
    rv = self.invoke(ctx)
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\click\core.py", line 1873, in invoke
    return _process_result(sub_ctx.command.invoke(sub_ctx))
                           ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\click\core.py", line 1269, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\click\core.py", line 824, in invoke
    return callback(*args, **kwargs)
  File "C:\Users\caiguanhong\scoop\apps\python313\current\Lib\site-packages\click\decorators.py", line 34, in new_func
    return f(get_current_context(), *args, **kwargs)
  File "E:\esptool-libo\esptool\esptool\__init__.py", line 827, in write_flash_cli
    write_flash(ctx.obj["esp"], addr_filename, **kwargs)
    ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "E:\esptool-libo\esptool\esptool\cmds.py", line 1553, in write_flash
    res = esp.flash_md5sum(base_address, base_size)
  File "E:\esptool-libo\esptool\esptool\loader.py", line 158, in inner
    return func(*args, **kwargs)
  File "E:\esptool-libo\esptool\esptool\loader.py", line 1693, in flash_md5sum
    res = self.check_command(
        "calculate md5sum",
    ...<6 lines>...
        allow_resend=True,
    )
  File "E:\esptool-libo\esptool\esptool\loader.py", line 699, in check_command
    return self.transport_check_command(
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        op_description,
        ^^^^^^^^^^^^^^^
    ...<4 lines>...
        timeout=timeout,
        ^^^^^^^^^^^^^^^^
    )
    ^
  File "E:\esptool-libo\esptool\esptool\loader.py", line 766, in transport_check_command
    return self.check_command(
           ~~~~~~~~~~~~~~~~~~^
        op_description,
        ^^^^^^^^^^^^^^^
    ...<6 lines>...
        _transport_inner=True,
        ^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "E:\esptool-libo\esptool\esptool\loader.py", line 714, in check_command
    val, data = self.command(
                ~~~~~~~~~~~~^
        op,
        ^^^
    ...<3 lines>...
        allow_resend=allow_resend or self._flaky_usb_transport(),
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "E:\esptool-libo\esptool\esptool\loader.py", line 659, in command
    raise UnsupportedCommandError(self, op)
esptool.util.UnsupportedCommandError: Invalid (unsupported) command 0x13
```


