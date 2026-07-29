# Self-Healing Agent

APP 自动化脚本自愈引擎。当元素定位失败时，自动调用 LLM 分析 UI 树找出正确的定位符，重试操作，并将修复回写到脚本文件。

## 支持的框架

| 平台 | 框架 | 定位方式 |
|------|------|---------|
| Android | uiautomator2 | resourceId / text / description / className |
| iOS | WebDriverAgent (wda) | name / label / value / type |
| HarmonyOS | hypium | text / id / key / hint / type |

## 快速开始

### 1. 安装依赖

```bash
pip install openai
```

### 2. 配置 LLM API（可选）

创建 `.env` 文件（已加入 `.gitignore`，不会提交）：

```ini
API_KEY=sk-your-api-key
BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o
```

也支持通过同名环境变量配置（优先级：`.env` < 环境变量）。

不配置则自动降级为启发式匹配（适用于简单的文字/ID 差异场景）。

### 3. 使用

#### uiautomator2

```python
import uiautomator2 as u2
from healer import auto_heal

d = u2.connect()
d = auto_heal(d, framework='uiautomator2', script_path='test_login.py')

# 正常写脚本，定位失败时自动愈合
d(resourceId="com.example:id/btn_login").click()
```

#### 使用回调持久化（数据库/内存）

```python
def on_healed(record):
    db.save_heal_record(record['intent'], record['new_locator'])

d = auto_heal(d, framework='uiautomator2', persist_callback=on_healed)
```

#### hypium

```python
from hypium import BY
from healer import auto_heal

d = UiDriver(...)
d = auto_heal(d, framework='hypium', script_path='test_main.py')

d.touch(BY.text("事件"))
```

#### wda

```python
from healer import auto_heal

d = wda.Client('http://localhost:8100')
d = auto_heal(d, framework='wda', script_path='test_ios.py')

d(label="Login").tap()
```

## 自愈流程

```
操作失败（元素定位异常）
  → 页面快照（DOM + 锚点）
  → LLM 诊断错误 + 生成候选定位符
  → 逐候选尝试 + 验证
  → 成功 → 回写脚本
  → 失败 → 回滚页面状态 → 下一个候选
```

- **LLM 模式**：分析语义意图，即使字符串完全不同也能正确匹配
- **启发式模式**（LLM 不可用时）：字符相似度 + OCR 噪声归一化，兜底简单差异
- **HealCache**：同一错误命中 3 次后跳过 LLM，直接使用历史愈合结果
- **因果链检测**：连续失败 2 次后自动回退到上一愈合点

## 配置文件

创建 `.env` 文件放在项目根目录（已 `.gitignore`）：

```ini
API_KEY=sk-xxx
BASE_URL=https://apihub.agnes-ai.com/v1
MODEL=agnes-2.0-flash
```

环境变量同名覆盖（`API_KEY` / `BASE_URL` / `MODEL`），或通过 `HEALER_CONFIG` 指定自定义路径。

## 项目结构

```
healer.py                     # 核心引擎（单文件，~1200 行）
SELF_HEALING_ARCHITECTURE.md  # 架构设计文档
.env                          # LLM API 配置（已 gitignore，需自行创建）
.env.example                  # 配置模板（可提交）
```

## 关键设计

- **不下发坐标**：自愈只修复元素定位符，操作方法和参数保持不变
- **不依赖截图/SSIM**：所有验证基于 DOM 结构变化
- **验证即回滚**：验证失败自动回滚页面状态，不阻塞后续候选
- **脚本回写**：自动识别脚本中的定位符写法并原地替换

## License

MIT
