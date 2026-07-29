# Self-Healing Agent 跨三端自动化脚本自愈方案

> 覆盖 Android (uiautomator2) / iOS (wda) / HarmonyOS (hypium)
> 版本: v2.0 — LLM 全流程单循环方案（替代原多验证器架构）

---

## 目录

1. [问题定义](#1-问题定义)
2. [总体架构](#2-总体架构)
3. [检测层 — 何时触发自愈](#3-检测层--何时触发自愈)
4. [推理层 — 如何找到正确元素](#4-推理层--如何找到正确元素)
5. [验证层 — 如何确认修复正确](#5-验证层--如何确认修复正确)
6. [回滚层 — 试错后如何恢复状态](#6-回滚层--试错后如何恢复状态)
7. [回写层 — 如何持久化修复](#7-回写层--如何持久化修复)
8. [三端适配](#8-三端适配)
9. [坐标处理策略](#9-坐标处理策略)
10. [完整自愈流程示例](#10-完整自愈流程示例)
11. [已知边界与应对](#11-已知边界与应对)
12. [代码优化建议](#12-代码优化建议)

---

## 1. 问题定义

### 1.1 解决的问题

AI 生成的自动化测试脚本中，元素定位符（resourceId / text / description / id / key 等）可能因以下原因失效：

| 错误类型 | 示例 | 来源 |
|---------|------|------|
| 拼写错误 | `"btn_login"` → 实际 `"sign_in_btn"` | AI 生成幻觉 |
| 语义近似 | `"AiII"` → 实际 `"AI"` | OCR/转录误差 |
| UI 变更 | 文本改了、ID 重构、层级调整 | 应用版本更新 |
| 跨设备差异 | 不同分辨率/ROM 的 ID 不同 | 多设备兼容 |

### 1.2 需要解决的核心矛盾

```
元素定位失败 → 自愈找到新定位符 → 用新定位符重试操作
                                      ↓
                                操作"成功"（没抛异常）
                                      ↓
                           但点的是否是正确的元素？
                                      ↓
                     用后续脚本中的定位符验证？❌
                      → 后续定位符也可能是错的（循环依赖）
```

**核心矛盾**：验证修复是否正确的依据，不能依赖同样可能出错的脚本定位符。

### 1.3 三个已知问题

| # | 问题 | 现状 | 方案 |
|---|------|------|------|
| ① | 坐标定位无法验证与修复 | 不同设备坐标不通用 | 跳过自愈，继续执行 |
| ② | 订正只有一次重试 | 最高分候选失败就放弃 | 引入多候选队列 |
| ③ | 无法确认订正行为是否准确 | 操作成功=修好了？不一定 | 引入独立验证层 |

---

## 2. 总体架构 — LLM 全流程单循环

### 设计原则

```
核心思想：LLM 是全流程唯一的"大脑"
所有决策只依赖两样东西：当前页面 DOM + 操作上下文（intent/old_locator/action）

不需要：视觉截图比对、SSIM、多验证器投票、Toast 捕获
```

### 循环结构

```
                     ┌──────────────────────────────────┐
                     │         Healing Loop              │
                     │  (max_total=5, 全局计数器)         │
                     │                                   │
              ┌──────▼──────┐    ┌──────────────────┐    │
              │  Step 1     │    │  Step 2          │    │
              │ LLM 诊断     │───→│ LLM 定位 + 执行   │───→│
              │ 为什么失败?   │    │ 找到正确元素并操作  │    │
              └──────┬──────┘    └────────┬─────────┘    │
                     │                    │               │
                     │    ┌───────────────▼──────────┐   │
                     │    │  Step 3                  │   │
                     └───→│ LLM 验证（一次调用完成）    │   │
                          │ 诊断+候选+预期变化+因果检查  │   │
                          └───────────┬──────────────┘   │
                                      │                   │
                               ┌──────▼──────┐           │
                               ▼              ▼           │
                           ✅符合预期     ❌不符合预期      │
                               │              │           │
                               │         ┌────▼────┐      │
                               │         │ Step 4  │      │
                               │         │ LLM 回滚│      │
                               │         └────┬────┘      │
                               │              │           │
                               ▼              ▼           │
                          继续执行    回到 Step 1 重试     │
                          (回写脚本)   (候选耗尽则失败)     │
                                      └──────────────────┘
```

### 一次 LLM 调用输出全部决策

```json
{
  "diagnosis": "element_not_found",
  "candidates": [
    {"by": "text", "value": "确定", "confidence": 0.85, "reasoning": "..."},
    {"by": "id", "value": "confirm_btn", "confidence": 0.45, "reasoning": "..."}
  ],
  "expected_change": "从配置页返回上一页，页面确定按钮消失，出现列表项",
  "expected_change_dir": "navigate",
  "fallback_action": "press_back",
  "causal_check": {
    "suggest_retry_prev_heal": false,
    "reason": ""
  }
}
```

### 收敛保证（防死循环）

```python
class HealingSession:
    MAX_TOTAL = 10           # 整次脚本总自愈上限
    MAX_PER_ELEMENT = 3      # 单元素定位符重试上限
    MAX_CONSECUTIVE_FAIL = 5 # 连续失败上限
```

### 四种终结态

| 终结态 | 含义 | 后续 |
|--------|------|------|
| `HEAL_SUCCEED` | 愈合成功，验证通过 | 回写脚本，继续执行 |
| `HEAL_EXHAUSTED` | 所有候选耗尽 | 抛异常（含 DOM + 尝试记录） |
| `HEAL_LOOP_DETECTED` | 同一元素反复失败 >3 次 | 跳过该元素，日志记录，继续下一行 |
| `HEAL_CHAIN_BROKEN` | 因果链断裂（上一步愈合可能错了） | 回退到上一个愈合点重做 |

---

## 3. 单一 Prompt 设计（核心）

整个自愈流程只需要 **一个 prompt 模板**，涵盖诊断 + 候选 + 预期 + 因果判断。

```python
HEAL_PROMPT = """You are a mobile UI self-healing agent.

## Context
Framework: {framework}
Failed action: {action}
Intent (what user wanted to find): {intent}
Old locator: {old_locator}

## Current page DOM (compressed)
{compressed_dom}

## Task
Analyze the failure and provide a self-healing plan.

Respond ONLY with JSON:
{{
  "diagnosis": "element_not_found" | "element_not_interactable" | "locator_wrong_type",
  "candidates": [
    {{"by": "text", "value": "...", "confidence": 0.0~1.0, "reasoning": "..."}}
  ],
  "expected_change": "Describe what page change should happen after correct action",
  "expected_change_dir": "navigate" | "stay" | "partial",
  "fallback_action": "press_back" | "restart_app" | null,
  "causal_check": {{
    "suggest_retry_prev_heal": false,
    "reason": "Only true if previous step's healing likely caused wrong page"
  }}
}}
"""
```

### 验证逻辑：LLM 自行判断操作前后页面变化

不需要单独的 Verifier 类。执行候选后，**再次调用同一个 prompt**（或简化的验证 prompt）：

```python
VERIFY_PROMPT = """You are a mobile UI testing verifier.

Before action (DOM before click):
{source_before[:3000]}

After action (DOM after click):
{source_after[:3000]}

The action performed: {action} on intent "{intent}"
Expected change: {expected_change}
Expected change direction: {expected_change_dir}

Respond ONLY with JSON:
{{
  "page_changed": true/false,
  "change_consistent": true/false,
  "confidence": 0.0~1.0,
  "reasoning": "..."
}}
"""
```

**通过规则：** `change_consistent=true` + `confidence>=0.4` → 通过。无需多验证器投票。

### DOM 压缩策略（修复点 #1：替代盲截断）

直接 `[:4000]` 截断可能砍掉区分相似元素的上下文。改为结构化压缩：

```python
def _compress_dom(self, source, framework):
    """
    压缩 DOM 到 <4000 字符，保留关键结构信息。
    """
    if framework in ('hypium', 'harmonyos') and source.strip().startswith('{'):
        return self._compress_json_dom(source)
    return self._compress_xml_dom(source)

def _compress_json_dom(self, source):
    data = json.loads(source)
    # 保留: text/id/key/type/clickable/enabled/accessibilityId/pagePath
    # 移除: bounds/opacity/zIndex/hashcode/origBounds/hitTestBehavior 等
    KEEP = {'text', 'id', 'key', 'type', 'clickable', 'enabled',
            'accessibilityId', 'hint', 'pagePath', 'bundleName',
            'description', 'label', 'name'}
    def strip(node):
        attrs = {k: v for k, v in node.get('attributes', {}).items() if k in KEEP and v}
        children = [strip(c) for c in node.get('children', [])]
        # 折叠空节点
        if not attrs and not children:
            return None
        result = attrs
        if children:
            result['children'] = [c for c in children if c]
        return result
    stripped = strip(data)
    # 递归限制深度到 8 层，防止 JSON 超长
    return self._truncate_depth(json.dumps(stripped, ensure_ascii=False), max_depth=8)

def _compress_xml_dom(self, source):
    # 移除 bounds/package/index 等无关属性
    source = re.sub(r'\s+(bounds|package|index|rotation|opacity|scrollable|focused)=["\'][^"\']*["\']', '', source)
    # 移除空节点
    source = re.sub(r'<node\s+(class=["\']\w+["\'])\s*/>', '', source)
    return source[:4000]  # XML 本身较干净，截断即可
```

### 动画稳定策略（修复点 #7）

```python
def _sample_dom_stable(self):
    """操作后等待动画稳定再采样 DOM"""
    time.sleep(0.5)
    dom1 = self.dump_hierarchy()
    time.sleep(0.3)
    dom2 = self.dump_hierarchy()
    # 如果两次 DOM 差异大，说明还在动画中，再等
    if difflib.SequenceMatcher(None, dom1, dom2).ratio() < 0.9:
        time.sleep(0.5)
        return self.dump_hierarchy()
    return dom2
```

### 跨运行愈合缓存（修复点 #6）

```python
import json, os

class HealCache:
    """跨脚本运行的愈合记忆层。避免同一错误每次跑都重复 LLM 调用。"""

    CACHE_PATH = os.path.join(os.path.dirname(__file__), '.heal_cache.json')

    def __init__(self):
        self._data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.CACHE_PATH):
            with open(self.CACHE_PATH, 'r') as f:
                self._data = json.load(f)

    def _save(self):
        with open(self.CACHE_PATH, 'w') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, intent, framework):
        """命中且愈合次数 >= 3 的缓存可跳过 LLM"""
        key = f"{framework}:{intent}"
        entry = self._data.get(key)
        if entry and entry.get('hit', 0) >= 3:
            return entry  # 直接复用历史愈合结果
        return None

    def record(self, intent, framework, candidate):
        key = f"{framework}:{intent}"
        if key in self._data:
            self._data[key]['hit'] += 1
            self._data[key]['last'] = __import__('time').time()
        else:
            self._data[key] = {
                'by': candidate['by'],
                'value': candidate['value'],
                'hit': 1,
                'last': __import__('time').time(),
            }
        self._save()
```

### 坐标跳过

```python
def _is_coordinate(self, locator):
    if isinstance(locator, tuple) and len(locator) == 2:
        return True
    if isinstance(locator, dict):
        for k in locator.keys():
            if k in ('x', 'y', 'pos', 'position'):
                return True
    return False
```

坐标操作失败时不自愈、不重试，直接抛异常。

---

## 4. 三端 UI 资源树格式

### 格式对比

| 端 | 获取方式 | 格式 | 解析方式 |
|---|---------|------|---------|
| **Android** | `d.dump_hierarchy()` | **XML** `<node text="..." class="..." .../>` | 正则匹配 `<node>` tag |
| **iOS** | `d.source()` | **XML** `<Button label="..." .../>` 各类型独立 tag | 正则匹配任意 tag |
| **HarmonyOS** | `d.dump_hierarchy()` / `UiTree(d).tree` | **JSON** 嵌套 `{attributes, children}` | `json.loads` + 递归 DFS |

### Android XML 示例

```xml
<node index="0" text="登录" class="android.widget.Button"
      resource-id="com.example.app:id/sign_in_btn"
      content-desc="" clickable="true" enabled="true"
      bounds="[100,800][980,960]"/>
```

解析为：`{text, resource_id, class_name, content_desc, clickable, enabled}`

### iOS XML 示例

```xml
<Button name="login_btn" label="登录" value="" type="Button" enabled="true"/>
```

解析为：`{type, label, value, name, enabled}`

### HarmonyOS JSON 示例

```json
{
  "attributes": {
    "type": "Button",
    "text": "确定",
    "id": "confirm_btn",
    "key": "",
    "hint": "点击确认",
    "clickable": "true",
    "enabled": "true",
    "bounds": "[0,0][1260,2720]",
    "pagePath": "pages/ConfirmPage",
    "bundleName": "com.loock.lockin_oh",
    "accessibilityId": "confirm_button"
  },
  "children": [...]
}
```

解析为：`{type, text, id, key, hint, enabled}`

**HarmonyOS 独有字段：**
- `pagePath` — 当前页面路由路径（如 `pages/Index`），可用作稳定锚点
- `bundleName` — 包名
- `hint` — 输入提示文本（当前 `healer.py` 未抽取，需补上）
- `accessibilityId` — 无障碍 ID（类似 Android content-desc）

### 解析逻辑（已有代码 `healer.py:102-156`）

```python
def _parse_hierarchy(self, source, framework):
    if framework in ('uiautomator2', 'android'):
        # 正则 <node .../> 提取 text/resource_id/class_name/content_desc
    elif framework in ('wda', 'ios'):
        # 正则任意 tag 提取 type/label/value/name
    elif framework in ('hypium', 'harmonyos'):
        if source.strip().startswith('{'):  # JSON 格式
            # json.loads → DFS 递归遍历 attributes/children
        else:  # XML 格式（旧版 hypium 兼容）
            # 正则提取 text/id/key/type
```

---

## 5. Aging Loop（核心自愈循环）

### HealSession 类

```python
class HealSession:
    """一次自愈尝试的完整生命周期"""

    MAX_TOTAL = 10           # 全局总自愈次数上限
    MAX_PER_ELEMENT = 3      # 同一定位符重试上限
    MAX_CONSECUTIVE_FAIL = 5 # 连续失败上限
    MIN_CONFIDENCE = 0.4     # 候选最低置信度

    def __init__(self):
        self.global_count = 0         # 全局计数器
        self.element_count = {}       # 按 intent 计次
        self.consecutive_fail = 0     # 连续失败计数器
        self.heal_chain = []          # 愈合链 [(before_dom, candidate, after_dom), ...]
        self.status = HealStatus.HEAL_SUCCEED

    def can_attempt(self, intent):
        return (
            self.global_count < self.MAX_TOTAL
            and self.element_count.get(intent, 0) < self.MAX_PER_ELEMENT
            and self.consecutive_fail < self.MAX_CONSECUTIVE_FAIL
        )
```

### 主循环实现

```python
def _heal_and_retry(self, action, *args):
    if not self._session.can_attempt(self._intent()):
        self._handle_loop_detected()
        return

    # 1. 获取操作前 DOM
    before_dom = self._get_formatted_dom()

    # 2. LLM 一次调用：诊断 + 候选 + 预期变化 + 因果检查
    heal_result = self._call_llm_heal(before_dom, action, self._intent(), self._locator)

    if not heal_result or not heal_result.get('candidates'):
        raise RuntimeError(f"Self-heal: no candidates for {self._locator}")

    # 3. 因果链检测
    if heal_result.get('causal_check', {}).get('suggest_retry_prev_heal'):
        self._rollback_to_previous_heal_point()
        return self._retry_previous_operation()

    # 4. 逐候选尝试
    for candidate in heal_result['candidates']:
        if candidate.get('confidence', 0) < self._session.MIN_CONFIDENCE:
            continue

        # 执行操作
        new_obj = self._driver(**{candidate['by']: candidate['value']})
        try:
            method = getattr(new_obj, action)
            result = method(*args) if args else method()
        except Exception:
            continue  # 操作异常 → 下一候选

        # 5. LLM 验证：对比操作前后 DOM
        after_dom = self._get_formatted_dom()
        verify_result = self._call_llm_verify(
            before_dom, after_dom,
            heal_result['expected_change'],
            heal_result['expected_change_dir'],
            self._intent()
        )

        if verify_result.get('change_consistent') and verify_result.get('confidence', 0) >= 0.4:
            # ✅ 验证通过
            self._session.heal_chain.append((before_dom, candidate, after_dom))
            self._session.consecutive_fail = 0
            self._patch_and_return(candidate, result)
            return result

        # ❌ 验证失败 → 回滚
        self._rollback(before_dom)

    # 6. 所有候选失败
    self._session.consecutive_fail += 1
    raise RuntimeError(f"Self-heal failed: all candidates exhausted for {self._locator}")
```

### 回滚策略（修复点 #2：截图 hash + 锚点 + DOM 三保险）

```python
def _rollback(self, target_ctx):
    """
    回滚到目标页面状态。
    target_ctx: {dom, anchor, screenshot_hash}
    三端各有稳定锚点:
      Android: activity + package
      iOS:     bundle_id + screen_title
      HarmonyOS: pagePath + bundleName
    """
    for _ in range(5):
        current_ctx = self._capture_page_ctx()
        if self._ctx_equal(current_ctx, target_ctx):
            return True
        if self._is_root_page(current_ctx):
            break
        self._press_back()
        time.sleep(0.5)

    return self._restart_app()

def _capture_page_ctx(self):
    """采集页面三要素：DOM + 锚点 + 截图 hash"""
    return {
        'dom': self._compress_dom(self.dump_hierarchy(), self.framework),
        'anchor': self._adapter.get_page_anchors(),  # 稳定锚点
        'screenshot_hash': self._screenshot_hash(),   # 截图 md5
    }

def _ctx_equal(self, a, b):
    """页面是否相同：锚点 > 截图 hash > DOM 文本"""
    if a['anchor'] == b['anchor']:
        return True  # 锚点相同 → 同一页面
    if a.get('screenshot_hash') and b.get('screenshot_hash'):
        if a['screenshot_hash'] == b['screenshot_hash']:
            return True  # 截图一致 → 同一页面
    # 兜底：DOM 文本相似度
    return difflib.SequenceMatcher(None, a['dom'], b['dom']).ratio() >= 0.7

def _screenshot_hash(self):
    """截图 md5，轻量比对"""
    try:
        img = self._adapter.screenshot()
        import hashlib
        return hashlib.md5(img.tobytes()).hexdigest()
    except Exception:
        return None
```

### 因果检测改为确定性机制（修复点 #5）

删除原来 LLM 反事实推理的 `causal_check` 字段，改为确定性检测：

```python
def _check_chain(self):
    """连续 2 元素失败 → 自动回退到上一个愈合点"""
    if self._session.consecutive_fail >= 2:
        if len(self._session.heal_chain) > 0:
            prev = self._session.heal_chain[-1]
            # 回退到上一个愈合前的页面
            self._rollback(prev['before_ctx'])
            # 从那里重新执行
            return self._retry_from(prev['candidate'])
    return False
```
        
        # 5. 获取操作后页面状态
        after_ctx = self.state_manager.snapshot()
        
        # 6. 验证
        action_info = {
            'intent': self._intent(),
            'framework': self.framework,
            'expected_change': self._expected_change(),
            'expected_change_dir': self._expected_change_dir(),
        }
        result = self.verifier.verify(before_ctx, after_ctx, action_info)
        
        if result == VerifiedResult.PASS:
            # 验证通过 → 提交修复
            self._commit_heal(candidate)
            return result
        
        # 7. 验证失败 → 回滚到操作前状态
        print(f"  [Heal] Candidate {i+1} verification failed, rolling back")
        if not self.state_manager.restore(before_ctx):
            print(f"  [Heal] Rollback failed, trying next candidate anyway")
    
    # 8. 所有候选失败
    raise RuntimeError(
        f"Self-heal failed: all {len(candidates)} candidates failed "
        f"for {self._locator}"
    )
```

---

## 6. 回写层

### 仅在验证通过后回写

```python
class ScriptPatcher:
    def __init__(self, script_path):
        self.script_path = script_path
        self.pending = []

    def record(self, heal_record):
        self.pending.append(heal_record)

    def commit(self, heal_record):
        """LLM 验证通过后，立即回写"""
        self._apply(heal_record)
        self.pending.remove(heal_record)

    def discard(self, heal_record):
        self.pending.remove(heal_record)

    def _apply(self, rec):
        content = open(self.script_path, 'r', encoding='utf-8').read()
        old_loc, new_loc = rec['old_locator'], rec['new_locator']

        patterns = [
            (f'{old_loc["by"]}="{old_loc["value"]}"',
             f'{new_loc["by"]}="{new_loc["value"]}"'),
            (f'BY.{old_loc["by"]}("{old_loc["value"]}")',
             f'BY.{new_loc["by"]}("{new_loc["value"]}"'),
            (old_loc['value'], new_loc['value']),
        ]
        for old, new in patterns:
            if old in content:
                content = content.replace(old, new, 1)
                break

        with open(self.script_path, 'w', encoding='utf-8') as f:
            f.write(content)
```

---

## 7. 三端适配

### 7.1 统一接口抽象

```python
class PlatformAdapter(ABC):
    @abstractmethod
    def dump_hierarchy(self) -> str: ...
    @abstractmethod
    def create_element(self, by, value): ...
    @abstractmethod
    def press_back(self): ...
    @abstractmethod
    def restart_app(self): ...
    @abstractmethod
    def get_page_anchors(self) -> dict: ...


class U2Adapter(PlatformAdapter):
    """Android uiautomator2"""
    def dump_hierarchy(self):  return self.d.dump_hierarchy()
    def press_back(self):      self.d.press("back")
    def create_element(self, by, value): return self.d(**{by: value})
    def get_page_anchors(self):
        return {'package': self.d.app_current().get('package', ''),
                'activity': self.d.app_current().get('activity', '')}

class WdaAdapter(PlatformAdapter):
    """iOS WebDriverAgent"""
    def dump_hierarchy(self):  return self.d.source()
    def press_back(self):      pass  # iOS 无统一 back，手势替代
    def create_element(self, by, value): return self.d(**{by: value})
    def restart_app(self):     self.d.close(); self.d.session().launch()

class HypiumAdapter(PlatformAdapter):
    """HarmonyOS hypium"""
    def dump_hierarchy(self):
        try:
            from hypium.uidriver.uitree import UiTree
            tree = UiTree(self.d); tree.refresh()
            return json.dumps(tree.tree, ensure_ascii=False)
        except Exception:
            return self.d.dump_hierarchy()
    def press_back(self):      self.d.press_back()
    def create_element(self, by, value):
        from hypium import BY
        return self.d.touch(getattr(BY, by)(value))
    def get_page_anchors(self):
        dom = json.loads(self.dump_hierarchy())
        attrs = dom.get('attributes', {})
        return {'bundleName': attrs.get('bundleName', ''),
                'pagePath': attrs.get('pagePath', '')}
```

### 7.2 定位符优先级（各端不同）

```
Android:   resourceId > text > content-desc > className
iOS:       name > label > value > type
HarmonyOS: id > key > text > type
```

### 7.3 各端元素解析对比

| 端 | 元素字段 | 独有字段 |
|---|---------|---------|
| Android | text, resource_id, class_name, content_desc, clickable, enabled | `resource_id`, `content_desc`, `clickable` |
| iOS | type, label, value, name, enabled | `label`, `name` |
| HarmonyOS | type, text, id, key, hint, enabled | `id`, `key`, `hint`, `pagePath`, `bundleName` |

---

## 8. 坐标处理策略

坐标操作不自愈、不重试，直接抛异常。

```python
def _is_coordinate(self, locator):
    if isinstance(locator, (tuple, list)) and len(locator) == 2:
        return True  # hypium: d.touch((143, 377))
    if isinstance(locator, dict):
        for k in locator:
            if k in ('x', 'y', 'pos', 'position'):
                return True  # u2: d(0.5, 0.5)
    return False
```

---

## 9. 完整自愈流程示例（以 "确定1" 场景）

```
操作: touch(BY.text("确定1")) → 异常 [Can't find component]
  │
  ▼
┌─ Step 1: LLM 诊断 ──────────────────────────────────────
│  LLM 看到 DOM 有 145 个节点
│  intent="确定1" → 分词理解 → "确定" + 多余 "1"
│  输出:
│    diagnosis: "element_not_found"
│    candidates: [
│      {by:"text", value:"确定", confidence:0.85,
│       reasoning: "确定1 contains 确定, typo extra 1"},
│      {by:"text", value:"17552309436的家", confidence:0.25},
│      {by:"id", value:"confirm_btn", confidence:0.45}
│    ]
│    expected_change: "点击确定后返回上一页"
│    expected_change_dir: "navigate"
└─────────────────────────────────────────────────────────
  │
  ▼
┌─ Step 2: 尝试候选 1 ────────────────────────────────────
│  touch(BY.text("确定")) → 操作成功
│  获取操作后 DOM
└─────────────────────────────────────────────────────────
  │
  ▼
┌─ Step 3: LLM 验证 ──────────────────────────────────────
│  before: "详细配置页：开关列表、确定按钮"
│  after:  "上一步骤页：列表页"
│  LLM: "点击确定后从配置页返回了上一页, consistent=true, confidence=0.85"
│  → ✅ 验证通过
└─────────────────────────────────────────────────────────
  │
  ▼
┌─ Step 4: 回写 ──────────────────────────────────────────
│  testHarmonyOS.py: "确定1" → "确定"
│  → 继续执行后续步骤
└─────────────────────────────────────────────────────────
```

### 如果候选 1 验证失败（点到电话号进了错误页面）

```
Step 3: LLM 验证
  before: "配置页"   after: "拨号页面"
  LLM: "点击确定不应进入拨号页面, consistent=false, confidence=0.9"
  → ❌ 验证不通过

Step 4: 回滚
  press_back() → 回到配置页

Step 2: 尝试候选 2 (id="confirm_btn", confidence=0.45)
  touch(BY.id("confirm_btn")) → 成功 → 回到上一页
  LLM 验证 → ✅ 通过 → 回写
```

---

## 10. 分支场景对照表

| # | 场景 | LLM 决策 | 结果 |
|---|------|---------|------|
| 1 | 元素拼写错误 (`确定1` → `确定`) | 语义理解匹配正确元素 | ✅ 愈合 |
| 2 | 元素完全变了 (`settingIcon` → `setup`) | DOM 中找语义最接近的元素 | ✅ LLM 理解 setup=设置 |
| 3 | 愈合后点到错误页面 | before/after DOM 对比不匹配预期 | ❌ 回滚，试下一候选 |
| 4 | 无有效候选（所有 confidence < 0.4） | candidates 为空 | ❌ 抛异常 |
| 5 | 连续两个元素都失败 | causal_check 链检测到上一步可能错了 | 🔄 回退上一个愈合点重做 |
| 6 | 同一元素反复失败 3 次 | element_count[intent] >= 3 | 🔄 HEAL_LOOP_DETECTED 跳过 |
| 7 | 全局愈合计次达 10 | global_count >= 10 | ❌ 抛异常，防死循环 |
| 8 | 页面有多个相似元素（两个"确定"） | LLM 根据上下文/坐标/父子关系分辨 | ✅ 选最合理的一个 |

---

## 11. 方案审查与修复点（实施前最终审核）

实施前已识别 7 个潜在漏洞并确定修复：

| # | 问题 | 严重度 | 修复方案 | 对应章节 |
|---|------|--------|---------|---------|
| 1 | DOM 盲截断 `[:4000]` 丢关键上下文 | ⚠️ 中 | 结构化压缩：保留语义属性、折叠空节点、限制深度 | §3 DOM 压缩 |
| 2 | 回滚后 `_dom_similar` 纯文本 diff 不可靠 | 🔴 高 | 三保险：锚点 + 截图 hash + DOM 文本 | §5 回滚策略 |
| 3 | 验证 prompt 重入幻觉（before/after 都有相同文案） | ⚠️ 中 | expected_change 改为结构变化描述，不只是文案 | §3 VERIFY_PROMPT |
| 4 | 每候选 2 次 LLM 调用，token 翻倍 | 🟡 低 | verify confidence 阈值降至 0.4（非 0.7），减少过度调用 | §3 通过规则 |
| 5 | causal_check 反事实推理不可靠 | 🔴 高 | 改为确定性检测：连续 2 次失败自动回退到上一个愈合点 | §5 因果检测 |
| 6 | 无跨运行记忆，每次重跑重复 LLM | 🟡 低 | `.heal_cache.json` 缓存，命中>=3 次跳过 LLM | §3 HealCache |
| 7 | 操作后动画导致 DOM 采样不准 | 🔴 高 | 双次采样 + 等待稳定 | §3 动画稳定 |

---

## 12. 代码优化建议（增量修改）

### 12.1 需要新增的组件

| 组件 | 说明 | 优先级 |
|------|------|--------|
| `HealSession` | 自愈会话（3 重计数器 + 愈合链 + 终结态） | P0 |
| `HEAL_PROMPT` / `VERIFY_PROMPT` | 两个 prompt 模板 | P0 |
| `PlatformAdapter` | 三端统一接口（dump/press_back/restart/anchors/screenshot） | P1 |
| `HealCache` | 跨运行愈合缓存（.heal_cache.json） | P1 |
| `DOMCompressor` | DOM 结构化压缩（去无关属性 + 深度限制） | P1 |

### 12.2 需要修改的现有代码

| 文件 | 修改内容 |
|------|---------|
| `healer.py:LLMHealingAgent` | 新增 `_heal_prompt()` / `_verify_prompt()` / `_compress_dom()` / `HealCache` 集成 |
| `healer.py:HealingUiObject._heal_and_retry` | 重写为多候选 + 验证 + 回滚 + 因果检测循环 |
| `healer.py:HypiumHealingProxy._heal_touch` | 移除 `< 0.2` 不一致阈值，统一使用 `HealSession` |
| `healer.py:ScriptPatcher._apply` | 增加 `BY.{by}("{value}")` 格式匹配 |
| `healer.py:_parse_hierarchy` | HarmonyOS 分支补上 `hint` 字段 |
| `healer.py:HealingProxy` | 集成 `PlatformAdapter` 替代原生 driver 调用 |

### 11.3 不需要修改的现有代码

- `load_config()` — 配置加载
- `_parse_hierarchy()` — 解析逻辑（只补 hint）
- `_to_locator()` — 定位符转换
- `MockDriver` / `MockUiObject` — mock
- `auto_heal()` — 入口接口

### 11.4 新增依赖

```python
# 不需要新增任何依赖，全流程只靠 LLM API + 内置 json/re/difflib
```

---

## 附录

### A. 真实运行日志分析

来自 `logs.txt` 的 HarmonyOS 测试：

| 步骤 | 定位符 | 当前结果 | 本方案会如何 |
|------|--------|---------|-------------|
| 1 | `BY.text("事1件")` | ✅ 自愈 → `text="事件"` score=0.37 | ✅ 一样愈合，且补上回写格式 |
| 2 | `(143, 377)` 坐标 | ✅ 直接执行 | ✅ 跳过，不自愈 |
| 3 | `BY.text("AiII")` | ✅ 自愈 → `text="AI"` score=0.31 | ✅ 一样愈合 |
| 4 | `BY.id("backBtn")` | ✅ 原生成功 | ✅ 不触发 |
| 5 | `(1117, 127)` 坐标 | ✅ 直接执行 | ✅ 跳过 |
| 6 | `BY.text("确定1")` | ❌ 误匹配电话号码 | ✅ **LLM 验证拦截**，回滚后试下一候选 |

### B. 与现有代码的兼容性

- `auto_heal()` 接口不变
- 无 LLM key 时降级为原有启发式评分（保留现有 `_score()` / `_mock_llm()`）
- 有 LLM key 时自动启用新单循环流程

---

> 文档版本: v2.0
> 最后更新: 2026-07-29
> 基于: healer.py 现有实现 + 三个系统的真实测试反馈 + LLM 全流程单循环方案