# 排查记录：USJ 不稳定链路丢响应与 `lost_response_resends`

> 这是问题排查 / 方案决策的记录（给自己留档）。面向使用者的用法说明见 `UPDATE_lost_response_resends.md`。

## 1. 问题现象

USB-Serial/JTAG（USJ）信号完整性差的自制板：能枚举（`lsusb` 见 `303a:1001`）、能进下载模式，但通信中途偶发报错：

```
A fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.
```

判别：同台电脑、同根线、同 USB 口，官方开发板正常、只有自制板异常 → 问题在板子 USB 物理层（D+/D- 走线、阻抗、串阻、去耦）。

## 2. 诊断（基于实测 trace）

trace 文件：`~/Share_Storage/esptool_trace.log`（**改动前的基线**，报的是 stock 文案，说明此时还没有 resend 逻辑）。

逐包分析结论——这条链路的故障签名**全部是「响应丢失 / 响应流水线错位」**：

- ROM 对 SYNC 会回 **8 个重复响应**；在坏链路上这些陈旧回包会溢出到**下一条命令**的读窗口。
- 于是后续每条命令（GET_SECURITY_INFO、READ_REG…）读到的都是上一条命令的陈旧回包，op 不匹配 → 全丢弃 → 排空后 3 秒超时 → `Serial data stream stopped`。
- **整份 trace 里没有任何一次 "Invalid command"**，即便在没有任何重试干预的裸奔状态下也没有。

关键推论：这块板子的实际故障 = 响应丢失，**不是** "请求被打坏 → ROM 回 invalid command"。

## 3. 解决方案

- 新增窄异常 `SerialReaderStoppedError(FatalError)`：仅表示「流空读中断」，**不含** panic / 非法 SLIP / 半包。
- `command()` / `check_command()` 增加 `allow_resend` opt-in：捕获 `SerialReaderStoppedError` 时 `flush_input()` + 重发同一请求，受 `LOST_RESPONSE_RESENDS` 次数上限约束。
- 只在**幂等**命令上开启：`sync` / `read_reg` / `get_security_info` / `flash_md5sum`。擦除 / 写入 / 改波特率 / 复位等状态变更命令**不开**（写 flash 走 `flash_block`，本就有 seq 幂等 + checksum + 整段重试）。
- 真错误（panic、真 `UnsupportedCommandError`、半包）立即抛，绝不被当成丢包重试掩盖。

## 4. 关键决策

### 4.1 删掉 `resend_on_invalid_command`
原本还有第二个开关，针对「请求在线路上被打坏 → ROM 回 invalid command（命令本应支持）」，仅用于 `SPI_FLASH_MD5`。

**删除理由**：现有实测 trace 里这条链路的故障签名全是丢响应，invalid-command 模式从未观测到 → 该开关缺乏实测依据，属于推测性兜底。
**盖棺所需证据**：一份带新逻辑、且能跑到 write-flash / MD5 阶段的 trace，确认 (a) 检测阶段能否靠 `allow_resend` 过去、(b) MD5 阶段是否真出现过 invalid-command。当前 trace 连芯片检测（READ_REG）都没过，走不到 MD5。

保留的不变式（有测试守门）：ROM 回 invalid command 时**始终立即**抛 `UnsupportedCommandError`，绝不重发——这样 `READ_REG` 的 invalid 回复（ESP32-S2 SDM 探测信号）和 `GET_SECURITY_INFO` 的能力探测结果都不会被误吞。

### 4.2 默认值改为 15（默认打开）
本 fork 把 `lost_response_resends` 默认从 0 改为 15，便于**零配置发给用户快速测试**。
- 代价：对正常会被拒的探测命令（如 ESP8266/初代 ESP32 的 `GET_SECURITY_INFO`）在丢响应时会重试到上限才放弃，少量额外延时；好链路不丢响应则不触发。
- 设 `lost_response_resends = 0` 恢复上游行为。
- **量产建议显式固定该值**（写进 `ESPTOOL_CFGFILE` 或 `~/.config/esptool/esptool.cfg`），别依赖隐式默认，防版本升级默认值变动影响一致性。

## 5. 推荐烧录姿势（不稳链路）

```bash
esptool --chip esp32c6 --no-stub -p /dev/ttyACM0 \
  --before usb-reset --after hard-reset \
  write-flash --flash-size detect \
  0x0     build/bootloader/bootloader.bin \
  0x8000  build/partition_table/partition-table.bin \
  0x10000 build/hello_world.bin
```

`--no-stub` 跳过无重试保护的 `mem_begin`/`mem_data`（坏链路最易崩的环节），改 ROM 直写。

## 6. 涉及文件

`esptool/util.py`（新异常）、`esptool/config.py`（注册配置项）、`esptool/loader.py`（默认 15 + allow_resend 逻辑）、`docs/en/esptool/configuration-file.rst`、`UPDATE_lost_response_resends.md`、`test/test_lost_response_resends.py`。

## 7. 结论 / 后续

- 本方案是**软件兜底**，不是根治；每次烧录仍在「赌」链路。稳定/批量烧录应修复板子 USB 物理设计。
- `resend_on_invalid_command` 已移除；若日后抓到 MD5 阶段确实出现 invalid-command 的 trace，可再评估是否加回。
