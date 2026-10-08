# 程序接入协议 v1

## 入口与依赖

推荐 `pip install .`，之后调用 `huozhiji-audit` 或导入 `huozhiji_audit`。核心三个函数不依赖机器人，也不打开浏览器：

| Python函数 | 输入 | 输出/副作用 |
|---|---|---|
| `convert_table(source, mapping=None, amount_rules=None, sheet=None)` | XLSX/CSV/TSV/JSON文件路径 | `{"rows": [...]}`；只读源文件，XLSX默认读所有产品sheet |
| `review(expected, observations)` | 数组或协议对象 | `{"schema_version":"1.0","summary":...,"results":[...]}`；不写文件 |
| `audit_workbook(source, observations, output, mapping=None, amount_rules=None)` | 原始XLSX、已有证据、不同的输出路径 | 报告加 `workbook` 元数据；保留源文件，原子写入输出XLSX |

可选参数是关键字参数。金额在SDK/报告中为两位小数字符串，待人审为 `null`；写Excel时为数值或空白。不要用二进制浮点累计金额。

审核使用源表结算金额，不要求评论里出现该金额。核对金额可见文字不是此SDK的默认业务口径。完整字段见 [数据协议](../skills/comment-review-audit/references/data-contract.md)。

## CLI

核心CLI标准输出只有一个JSON对象，退出码：0为处理完成，1为输入/运行错误，2仅表示 `review`/`audit --strict` 存在失败或待人审。0不代表所有评论成功，应读取 `summary.triage_counts`。

在线 `run` 的进度和验证提示输出到stderr、完成清单输出到stdout，遇到未解决的验证返回3；已解决的验证子进程返回4，由runner恢复断点。依赖未安装或内部错误不会被描述成“审核成功”。`maintain` 输出其原生JSON计划。

```python
import json
import subprocess

proc = subprocess.run([
    "huozhiji-audit", "audit", "--source", "评论.xlsx",
    "--observations", "observations.json", "--output", "审核版.xlsx",
    "--strict",
], capture_output=True, text=True, check=False)
payload = json.loads(proc.stdout)
if proc.returncode not in (0, 2):
    raise RuntimeError(payload)
# 读取payload["results"]逐条结果，不能只依据退出码结算。
```

自定义表头使用 `--map target_url=链接 --map comment_text=评论内容`，或SDK `mapping={"target_url":"链接","comment_text":"评论内容"}`。`--amount-rules text=1.5,text+image=2,image=1.5` 仅在来源明确授权该价格规则时使用，不要凭空定价。

## 证据与调用方责任

传入的 `observations` 必须由真实只读采集或人工核对产生。`capture_status: ok`、`capture_complete: true` 和 `image_detection_complete` 是可信采集者的声明，不是API对伪造输入的鉴别。SDK不连接平台二次验证这些声明；保留真实截图、时间和采集来源，并在业务端限制谁能提交证据。

公开源码不等于共用账号。每个使用者保管自己的账号/profile，不能共享cookie或把登录态放到仓库。Web服务若封装此工具，应自行做鉴权、文件大小限制、路径隔离、上传校验和队列控制，不要把原始命令行或任意路径参数直接暴露为公开接口。

在线模式只在正常授权会话下运行。遇到安全验证后必须由账号所有者正常处理；没有可信完整证据就不能判失败。无人值守环境遇到验证应标记任务blocked，而不是无限重试。

## Excel 引擎

独立引擎直接修改OOXML，不重建媒体或cell-image定义。原数据sheet只写G/H；汇总使用14列结算模板和P:X追踪明细，源表数字金额、汇总计数及公式缓存在导出后重新读取核对。Excel打开时重新计算。

独立引擎不提供任意Excel公式重算或PNG渲染；需要这些能力的宿主可显式选 `artifact` 引擎。不能安全保存的旧汇总绘图、保护表、签名文件和越界合并会报错，由人工处理，不会悄悄清空。

`output` 可以覆盖先前的成品路径，但不能等于源文件。任务重审请继续使用原始输入文件，不要将上次已经回填的H列当作新的报价。

## 仓库写入权限

本仓库公开供查看、安装与调用；上游修改由仓库所有者管理，不增加外部collaborator写权限。其他用户的fork/PR不是对上游的直接写入，只有所有者决定是否合入。公开仓库本身并不会把该仓库的机器人、账号或本地文件授权给别人。
