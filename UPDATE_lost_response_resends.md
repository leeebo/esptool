# 更新说明：`lost_response_resends`（容忍不稳定传输链路）

本 fork 在 esptool 基础上新增了一个配置项 **`lost_response_resends`**，用于在**不稳定的通信链路**上把命令"重发"到成功为止，常见于 **USB-Serial/JTAG（USJ）信号质量较差的自制板**。

- 安装来源：<https://github.com/leeebo/esptool>
- **本 fork 默认已打开（`15`）**，装好直接用、无需任何配置，便于发给用户快速测试。
  - 如需恢复上游原始行为（默认关闭），在配置文件里写 `lost_response_resends = 0` 即可。

---

## 1. 解决什么问题

在 USJ 信号完整性不佳的板子上（例如 D+/D- 走线、阻抗、串阻、去耦设计不当），USB 链路会**双向偶发丢包 / 传坏数据**，但设备仍能枚举。典型现象：

- 能枚举（`lsusb` 看得到 `303a:1001`）、能进下载模式；
- 但通信中途报错 `A fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.`（**响应包丢失**）——本配置项正是针对这种情况。

> 判别要点：把**官方开发板**插到同一台电脑、同一根线、同一个 USB 口测试。官方板正常、只有自制板异常 → 问题在板子的 USB 物理层，本配置项可作为软件兜底。

## 2. 它做了什么

开启后，当某条命令的**响应在链路上丢失**（等不到芯片回包，平时会直接报 `Serial data stream stopped`）时，esptool 会**自动重发同一条请求**并继续等待，直到收到正确响应或达到设定的重发次数上限。

`lost_response_resends` 就是**每条命令允许的最大重发次数**（本 fork 默认 `15`；设为 `0` 即关闭，恢复上游行为）。

为保证安全，自动重发只用于**幂等**的握手/只读类命令（连接同步、读寄存器、读安全信息、读 flash 校验值等）；擦除、写入、改波特率、复位这类**会改变状态**的命令不会被自动重发。真正的错误——芯片崩溃（panic）、命令确实不被支持等——仍会**立即报错**，不会被反复重试掩盖。

## 3. 安装

```bash
# 方式一：直接装这个 fork
pip install "git+https://github.com/leeebo/esptool.git"

# 方式二：克隆后以可编辑模式安装（便于本地改动/调试）
git clone https://github.com/leeebo/esptool.git
pip install -e ./esptool
```

> 若在 ESP-IDF 环境里使用，请在 **IDF 的 Python 虚拟环境**中执行上述安装，使其覆盖 IDF 自带的 esptool；可用
> `python -c "import esptool, os; print(os.path.dirname(esptool.__file__))"` 确认实际加载的是本 fork。

## 4. 用法（可选：用配置文件微调）

本 fork 已默认开启，**直接烧录即可**，无需任何配置。只有在你想**改重发次数**、**关掉它**、或**顺带调大其他重试**时，才需要放一个 `esptool.cfg`：

```ini
[esptool]
# 每条命令最多重发次数（本 fork 默认 15；设为 0 关闭，恢复上游行为）
lost_response_resends = 15
# 顺便把写块/连接/开口的重试也拉大，进一步提升不稳定链路下的成功率
write_block_attempts = 10
connect_attempts = 10
open_port_attempts = 10
```

配置文件查找顺序：**当前目录 → 用户目录**，文件名可为 `esptool.cfg` / `setup.cfg` / `tox.ini`（需含 `[esptool]` 段）。也可用环境变量 `ESPTOOL_CFGFILE=/path/to/esptool.cfg` 显式指定。

### 烧录命令（USJ 不稳定链路推荐姿势）

```bash
esptool --chip esp32c6 --no-stub -p /dev/ttyACM0 \
  --before usb-reset --after hard-reset \
  write-flash --flash-size detect \
  0x0     build/bootloader/bootloader.bin \
  0x8000  build/partition_table/partition-table.bin \
  0x10000 build/hello_world.bin
```

要点：

- **`--no-stub`**：跳过把 stub flasher 上传到 RAM 的步骤（`mem_begin`/`mem_data` 没有重试保护，是不稳定链路上最容易崩的环节），改用 ROM 直写。
- 三个镜像分别以 `Hash of data verified.` 结束即写入正确；`--after hard-reset` 让芯片启动。
- USJ 是 USB-CDC，`-b` 波特率对物理速率**无意义**，可省略。

## 5. 注意事项

- **本 fork 默认打开（`15`）。** 这是为了方便发给用户快速测试不稳链路；正常芯片/好链路下功能依旧不变，只是在确实丢响应时多花几次重试。
  - 代价：开启后对**正常会被拒的探测命令**（如 ESP8266/初代 ESP32 每次连接都会失败的 `GET_SECURITY_INFO`，因丢响应时会重试到上限再放弃）会引入少量额外往返与延时。若要零额外开销/对齐上游行为，在配置文件里设 `lost_response_resends = 0`。
  - 量产/批量烧录建议显式固定该值（在 `ESPTOOL_CFGFILE` 或系统级配置里写明），不要依赖隐式默认，以免日后升级版本时默认值变化影响一致性。

