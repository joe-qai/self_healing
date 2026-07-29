"""
Self-healing engine for APP automation scripts.
透明拦截原生 uiautomator2 / wda / hypium API，失败时调 LLM 自愈 + 回写脚本。
v2.0 — LLM 全流程单循环方案
"""
import difflib
import os
import re
import json
import inspect
import time
import hashlib
from enum import Enum
from abc import ABC, abstractmethod


# ── 配置加载 ────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), ".env")


def load_env_file(path):
    """Parse a .env file and return a dict of key=value pairs."""
    conf = {}
    if not os.path.exists(path):
        return conf
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            conf[key.strip()] = val.strip().strip('"').strip("'")
    return conf


def load_config(config_path=None):
    conf = {
        'api_key': '',
        'base_url': 'https://apihub.agnes-ai.com/v1',
        'model': 'agnes-2.0-flash',
    }
    path = config_path or os.environ.get('HEALER_CONFIG', DEFAULT_CONFIG_PATH)
    env = load_env_file(path)
    conf['api_key'] = env.get('API_KEY', '') or os.environ.get('API_KEY', '') or os.environ.get('OPENAI_API_KEY', '')
    conf['base_url'] = env.get('BASE_URL', '') or os.environ.get('BASE_URL', conf['base_url'])
    conf['model'] = env.get('MODEL', '') or os.environ.get('MODEL', conf['model'])
    return conf


# ── HealStatus ─────────────────────────────────────────────────────

class HealStatus(Enum):
    HEAL_SUCCEED = 'heal_succeed'
    HEAL_EXHAUSTED = 'heal_exhausted'
    HEAL_LOOP_DETECTED = 'heal_loop_detected'
    HEAL_CHAIN_BROKEN = 'heal_chain_broken'


# ── HealSession ────────────────────────────────────────────────────


class HealSession:
    """一次自愈尝试的完整生命周期：3 重计数器 + 愈合链 + 终结态。"""

    MAX_TOTAL = 10
    MAX_PER_ELEMENT = 3
    MAX_CONSECUTIVE_FAIL = 5
    MIN_CONFIDENCE = 0.4

    def __init__(self):
        self.global_count = 0
        self.element_count = {}
        self.consecutive_fail = 0
        self.heal_chain = []
        self.status = HealStatus.HEAL_SUCCEED

    def can_attempt(self, intent):
        return (
            self.global_count < self.MAX_TOTAL
            and self.element_count.get(intent, 0) < self.MAX_PER_ELEMENT
            and self.consecutive_fail < self.MAX_CONSECUTIVE_FAIL
        )

    def record_attempt(self, intent):
        self.global_count += 1
        self.element_count[intent] = self.element_count.get(intent, 0) + 1

    def record_success(self, before_ctx, candidate, after_ctx):
        self.consecutive_fail = 0
        self.heal_chain.append({
            'before_ctx': before_ctx,
            'candidate': candidate,
            'after_ctx': after_ctx,
        })
        self.status = HealStatus.HEAL_SUCCEED

    def record_fail(self):
        self.consecutive_fail += 1
        if self.consecutive_fail >= self.MAX_CONSECUTIVE_FAIL:
            self.status = HealStatus.HEAL_CHAIN_BROKEN


# ── HealCache ───────────────────────────────────────────────────────


class HealCache:
    """跨运行愈合记忆层。同一 intent 命中 >= 3 次后跳过 LLM。"""

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
        key = f"{framework}:{intent}"
        entry = self._data.get(key)
        if entry and entry.get('hit', 0) >= 3:
            return {'by': entry['by'], 'value': entry['value'],
                    'confidence': 0.9, 'reasoning': f'cache hit ({entry["hit"]} times)'}
        return None

    def record(self, intent, framework, candidate):
        key = f"{framework}:{intent}"
        if key in self._data:
            self._data[key]['hit'] += 1
            self._data[key]['last'] = time.time()
        else:
            self._data[key] = {
                'by': candidate['by'],
                'value': candidate['value'],
                'hit': 1,
                'last': time.time(),
            }
        self._save()


# ── DOMCompressor ────────────────────────────────────────────────────


class DOMCompressor:
    """DOM 结构化压缩，替代盲截断。保留语义属性，折叠空节点，限制深度。"""

    KEEP_ATTRS = {'text', 'id', 'key', 'type', 'clickable', 'enabled',
                  'accessibilityId', 'hint', 'pagePath', 'bundleName',
                  'description', 'label', 'name', 'resource_id',
                  'class_name', 'content_desc', 'value'}
    MAX_DEPTH = 8

    @classmethod
    def compress(cls, source, framework):
        if framework in ('hypium', 'harmonyos') and source.strip().startswith('{'):
            return cls._compress_json(source)
        return cls._compress_xml(source)

    @classmethod
    def _compress_json(cls, source):
        try:
            data = json.loads(source)
        except Exception:
            return source[:4000]

        def strip_node(node, depth=0):
            if depth > cls.MAX_DEPTH:
                return None
            attrs = node.get('attributes', {})
            kept = {k: v for k, v in attrs.items() if k in cls.KEEP_ATTRS and v}
            children = node.get('children', [])
            kept_children = [strip_node(c, depth + 1) for c in children]
            kept_children = [c for c in kept_children if c]
            if not kept and not kept_children:
                return None
            result = kept
            if kept_children:
                result['children'] = kept_children
            return result

        stripped = strip_node(data)
        if not stripped:
            return source[:4000]
        result = json.dumps(stripped, ensure_ascii=False)
        return result[:4000]

    @classmethod
    def _compress_xml(cls, source):
        source = re.sub(
            r'\s+(bounds|package|index|rotation|opacity|scrollable|'
            r'focused|longClickable|checkable|checked|selected)="[^"]*"',
            '', source
        )
        source = re.sub(r'<node\s+(class="\w+")\s*/>', '', source)
        return source[:4000]


# ── PlatformAdapter (三端统一接口) ──────────────────────────────────


class PlatformAdapter(ABC):
    @abstractmethod
    def dump_hierarchy(self): ...

    @abstractmethod
    def create_element(self, by, value): ...

    @abstractmethod
    def press_back(self): ...

    @abstractmethod
    def restart_app(self): ...

    @abstractmethod
    def get_page_anchors(self): ...

    @abstractmethod
    def screenshot(self): ...


class U2Adapter(PlatformAdapter):
    def __init__(self, driver):
        self.d = driver

    def dump_hierarchy(self):
        return self.d.dump_hierarchy()

    def create_element(self, by, value):
        return self.d(**{by: value})

    def press_back(self):
        self.d.press("back")

    def restart_app(self):
        pkg = self.d.app_current().get('package', '')
        if pkg:
            self.d.app_stop(pkg)
            time.sleep(1)
            self.d.app_start(pkg)

    def get_page_anchors(self):
        try:
            info = self.d.app_current()
            return {'package': info.get('package', ''), 'activity': info.get('activity', '')}
        except Exception:
            return {}

    def screenshot(self):
        return self.d.screenshot()


class WdaAdapter(PlatformAdapter):
    def __init__(self, driver):
        self.d = driver

    def dump_hierarchy(self):
        return self.d.source()

    def create_element(self, by, value):
        return self.d(**{by: value})

    def press_back(self):
        pass

    def restart_app(self):
        self.d.close()
        time.sleep(1)
        self.d.session().launch()

    def get_page_anchors(self):
        return {}

    def screenshot(self):
        return self.d.screenshot()


class HypiumAdapter(PlatformAdapter):
    def __init__(self, driver):
        self.d = driver

    def dump_hierarchy(self):
        try:
            from hypium.uidriver.uitree import UiTree
            tree = UiTree(self.d)
            tree.refresh()
            if tree.tree:
                return json.dumps(tree.tree, ensure_ascii=False)
        except Exception:
            pass
        return self.d.dump_hierarchy() if hasattr(self.d, 'dump_hierarchy') else None

    def create_element(self, by, value):
        try:
            from hypium import BY as HypiumBy
            try:
                method = getattr(HypiumBy, by)
            except AttributeError:
                return self.d.touch(value)
            if callable(method):
                return self.d.touch(method(value))
        except ImportError:
            pass
        return self.d.touch(value)

    def press_back(self):
        self.d.press_back()

    def restart_app(self):
        pass

    def get_page_anchors(self):
        src = self.dump_hierarchy()
        if src and src.strip().startswith('{'):
            try:
                data = json.loads(src)
                attrs = data.get('attributes', {})
                return {'bundleName': attrs.get('bundleName', ''),
                        'pagePath': attrs.get('pagePath', '')}
            except Exception:
                pass
        return {}

    def screenshot(self):
        return None


def build_adapter(driver, framework):
    if framework in ('uiautomator2', 'android'):
        return U2Adapter(driver)
    if framework in ('wda', 'ios'):
        return WdaAdapter(driver)
    if framework in ('hypium', 'harmonyos'):
        return HypiumAdapter(driver)
    return None


# ── LLM Healing Prompt ──────────────────────────────────────────────


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
    {{"by": "text|id|key|resourceId|description|name|label|className|type",
      "value": "...", "confidence": 0.0~1.0, "reasoning": "..."}}
  ],
  "expected_change": "Describe what page change should happen after correct action",
  "expected_change_dir": "navigate" | "stay" | "partial",
  "fallback_action": "press_back" | "restart_app" | null
}}
"""


VERIFY_PROMPT = """You are a mobile UI testing verifier.

Before action (DOM before click):
{source_before}

After action (DOM after click):
{source_after}

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


# ── LLM Healing Agent ──────────────────────────────────────────────


class LLMHealingAgent:
    """用 LLM 分析 UI hierarchy，找出正确的元素定位符。"""

    def __init__(self, config=None, cache=None):
        self.config = config or load_config()
        self._llm_available = bool(self.config.get('api_key'))
        self._cache = cache or HealCache()

    def heal(self, page_source, intent, old_locator, framework, action='click'):
        if self._llm_available:
            return self._call_llm(page_source, intent, old_locator, framework, action)
        return self._mock_llm(page_source, intent, old_locator, framework)

    def verify(self, source_before, source_after, intent, action,
               expected_change, expected_change_dir, framework):
        if self._llm_available:
            return self._call_llm_verify(source_before, source_after, intent,
                                          action, expected_change, expected_change_dir)
        return self._mock_verify(source_before, source_after, expected_change_dir)

    def _call_llm(self, source, intent, old_locator, framework, action='click'):
        cached = self._cache.get(intent, framework)
        if cached:
            self._llm_available = False
            return {
                'diagnosis': 'cache_hit',
                'candidates': [{**cached}],
                'expected_change': '',
                'expected_change_dir': 'navigate',
                'fallback_action': 'press_back',
            }

        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=self.config['api_key'],
                base_url=self.config['base_url'],
                timeout=5,
            )
            compressed = DOMCompressor.compress(source, framework)
            prompt = HEAL_PROMPT.format(
                framework=framework,
                action=action,
                intent=intent,
                old_locator=old_locator,
                compressed_dom=compressed,
            )
            resp = client.chat.completions.create(
                model=self.config['model'],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=600,
                timeout=5,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
            result = json.loads(text)
            candidates = result.get('candidates', [])
            for c in candidates:
                c['confidence'] = float(c.get('confidence', 0))
            return result
        except Exception as e:
            print(f"  [LLM] API call failed: {e}, fallback to heuristic")
            self._llm_available = False
            return self._mock_llm(source, intent, old_locator, framework)

    def _call_llm_verify(self, source_before, source_after, intent, action,
                         expected_change, expected_change_dir):
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=self.config['api_key'],
                base_url=self.config['base_url'],
                timeout=5,
            )
            prompt = VERIFY_PROMPT.format(
                source_before=DOMCompressor.compress(source_before, 'android')[:2000],
                source_after=DOMCompressor.compress(source_after, 'android')[:2000],
                action=action,
                intent=intent,
                expected_change=expected_change,
                expected_change_dir=expected_change_dir,
            )
            resp = client.chat.completions.create(
                model=self.config['model'],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                timeout=5,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
            result = json.loads(text)
            result['confidence'] = float(result.get('confidence', 0))
            return result
        except Exception as e:
            print(f"  [LLM] Verify API call failed: {e}, fallback to heuristic")
            self._llm_available = False
            return self._mock_verify(source_before, source_after, expected_change_dir)

    def _mock_llm(self, source, intent, old_locator, framework):
        elements = self._parse_hierarchy(source, framework)
        if not elements:
            return None
        print(f"  [Heal] 意图: '{intent}' 扫描 {len(elements)} 节点...")
        for el in elements:
            el['_score'] = self._score(el, intent, old_locator)
        best = max(elements, key=lambda x: x['_score'])
        if best['_score'] >= 0.3:
            loc = self._to_locator(best, framework)
            print(f"  [Heal] 最佳: {loc['by']}=\"{loc['value']}\" (score={best['_score']:.2f})")
            return {
                'diagnosis': 'element_not_found',
                'candidates': [{**loc, 'confidence': best['_score'], 'reasoning': 'heuristic match'}],
                'expected_change': '',
                'expected_change_dir': 'navigate',
                'fallback_action': 'press_back',
            }
        print(f"  [Heal] 无匹配 (best={best['_score']:.2f})")
        return None

    def _mock_verify(self, source_before, source_after, expected_change_dir):
        ratio = difflib.SequenceMatcher(
            None,
            DOMCompressor.compress(source_before, 'android'),
            DOMCompressor.compress(source_after, 'android'),
        ).ratio()

        if expected_change_dir == 'navigate':
            consistent = ratio < 0.7
        elif expected_change_dir == 'stay':
            consistent = ratio > 0.85
        else:
            consistent = 0.3 < ratio < 0.85

        return {
            'page_changed': ratio < 0.85,
            'change_consistent': consistent,
            'confidence': 0.5 if consistent else 0.2,
            'reasoning': f'DOM similarity={ratio:.2f}',
        }

    def _parse_hierarchy(self, source, framework):
        elements = []
        if framework in ('uiautomator2', 'android'):
            for tag in re.findall(r'<node\s+([^>]+?)/?>', source):
                attrs = dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', tag))
                if not attrs.get('class'):
                    continue
                elements.append({
                    'text': attrs.get('text', ''),
                    'resource_id': attrs.get('resource-id', attrs.get('resourceId', '')),
                    'class_name': attrs.get('class', ''),
                    'content_desc': attrs.get('content-desc', attrs.get('content_desc', '')),
                    'clickable': attrs.get('clickable', 'false') == 'true',
                    'enabled': attrs.get('enabled', 'true') == 'true',
                })
        elif framework in ('wda', 'ios'):
            for tname, tag in re.findall(r'<(\w+)\s+([^>]+?)/?>', source):
                attrs = dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', tag))
                elements.append({
                    'type': tname,
                    'label': attrs.get('label', ''),
                    'value': attrs.get('value', ''),
                    'name': attrs.get('name', attrs.get('label', '')),
                    'enabled': attrs.get('enabled', 'true') == 'true',
                })
        elif framework in ('hypium', 'harmonyos'):
            if source.strip().startswith('{'):
                try:
                    data = json.loads(source)
                    stack = [data]
                    while stack:
                        node = stack.pop()
                        attrs = node.get('attributes', {})
                        elements.append({
                            'type': attrs.get('type', node.get('type', '')),
                            'text': attrs.get('text', attrs.get('value', '')),
                            'id': attrs.get('id', attrs.get('ohos:id', '')),
                            'key': attrs.get('key', ''),
                            'hint': attrs.get('hint', ''),
                            'enabled': attrs.get('enabled', 'true') == 'true',
                        })
                        for child in reversed(node.get('children', [])):
                            stack.append(child)
                except Exception:
                    pass
            else:
                for tname, tag in re.findall(r'<([\w:]+)\s+([^>]+?)/?>', source):
                    attrs = dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', tag))
                    elements.append({
                        'type': tname,
                        'text': attrs.get('text', attrs.get('value', '')),
                        'id': attrs.get('id', attrs.get('ohos:id', '')),
                        'key': attrs.get('key', ''),
                        'hint': attrs.get('hint', ''),
                        'enabled': attrs.get('enabled', 'true') == 'true',
                    })
        return elements

    def _ocr_normalize(self, s):
        """Normalize OCR noise: collapse repeated chars, handle l/I confusion."""
        s = s.lower()
        s = re.sub(r'(.)\1{2,}', r'\1', s)
        s = re.sub(r'(?<=[a-z])l{2,}(?=[a-z])', r'l', s)
        s = s.replace('0', 'o').replace('1', 'l').replace('5', 's')
        return s

    def _score(self, element, intent, old_locator):
        score = 0.0
        il = intent.lower()
        norm_il = self._ocr_normalize(intent)
        kw = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+', il)
        norm_kw = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+', norm_il)
        text_pool = ' '.join(str(v) for v in element.values() if isinstance(v, str)).lower()
        norm_pool = self._ocr_normalize(text_pool)

        for k, nk in zip(kw, norm_kw):
            if k in text_pool or nk in norm_pool:
                score += 0.3 / max(len(kw), 1)
            else:
                parts = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|\d+', k)
                matched = False
                for p in parts:
                    if len(p) > 2 and (p.lower() in text_pool or self._ocr_normalize(p) in norm_pool):
                        score += 0.2 / max(len(kw), 1)
                        matched = True
                if not matched and len(k) > 1:
                    best_ratio = max(
                        (difflib.SequenceMatcher(None, k, w).ratio() for w in text_pool.split()),
                        default=0
                    )
                    norm_ratio = max(
                        (difflib.SequenceMatcher(None, nk, w).ratio() for w in norm_pool.split()),
                        default=0
                    ) if nk != k else 0
                    if best_ratio >= 0.6 or norm_ratio >= 0.6:
                        score += 0.15 / max(len(kw), 1)
        for _, v in old_locator.items():
            if isinstance(v, str) and len(v) > 3:
                vl = v.lower()
                padded = re.sub(r'(?<=[\u4e00-\u9fff])(?=[a-zA-Z0-9])', ' ', vl)
                padded = re.sub(r'(?<=[a-zA-Z0-9])(?=[\u4e00-\u9fff])', ' ', padded)
                padded = re.sub(r'(?<=[a-zA-Z])(?=\d)', ' ', padded)
                padded = re.sub(r'(?<=\d)(?=[a-zA-Z])', ' ', padded)
                for p in re.split(r'[./_\s]+', padded):
                    if len(p) > 1 and (p in text_pool or self._ocr_normalize(p) in norm_pool):
                        score += 0.08
        if element.get('clickable') and '点击' in il:
            score += 0.2
        if element.get('enabled'):
            score += 0.1
        ratio = max(
            difflib.SequenceMatcher(None, il, text_pool).ratio(),
            difflib.SequenceMatcher(None, norm_il, norm_pool).ratio(),
        )
        if ratio >= 0.3:
            score += ratio * 0.2
        return min(score, 2.0)

    def _to_locator(self, el, framework):
        if framework in ('uiautomator2', 'android'):
            if el.get('resource_id'):
                return {'by': 'resourceId', 'value': el['resource_id']}
            if el.get('text'):
                return {'by': 'text', 'value': el['text']}
            if el.get('content_desc'):
                return {'by': 'description', 'value': el['content_desc']}
            return {'by': 'className', 'value': el.get('class_name', '')}
        if framework in ('wda', 'ios'):
            if el.get('name'):
                return {'by': 'name', 'value': el['name']}
            if el.get('label'):
                return {'by': 'label', 'value': el['label']}
            if el.get('value'):
                return {'by': 'value', 'value': el['value']}
            return {'by': 'type', 'value': el.get('type', '')}
        if framework in ('hypium', 'harmonyos'):
            if el.get('id'):
                return {'by': 'id', 'value': el['id']}
            if el.get('key'):
                return {'by': 'key', 'value': el['key']}
            if el.get('text'):
                return {'by': 'text', 'value': el['text']}
            return {'by': 'type', 'value': el.get('type', '')}
        return {'by': 'unknown', 'value': ''}


# ── Script Patcher ─────────────────────────────────────────────────


class ScriptPatcher:
    """将愈合记录持久化到脚本。

    支持两种模式：
    - `script_path`：直接读写本地 .py 文件，原地替换定位符
    - `persist_callback`：回调函数，收到愈合记录后由调用方自行处理（如写入数据库/内存）
    """

    def __init__(self, script_path=None, persist_callback=None):
        self.script_path = script_path
        self.persist_callback = persist_callback

    def patch(self, heal_record):
        if not heal_record:
            return
        if self.persist_callback:
            self.persist_callback(heal_record)
            return
        if not self.script_path:
            return
        try:
            with open(self.script_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            print(f"  ! Script not found: {self.script_path}")
            return

        old_loc = heal_record.get('old_locator', {})
        new_loc = heal_record.get('new_locator', {})
        by_old = old_loc.get('by', list(old_loc.keys())[0] if old_loc else '')
        oldv = old_loc.get('value', old_loc.get(by_old, ''))
        by_new = new_loc.get('by', '')
        newv = new_loc.get('value', '')

        patterns = [
            (f'{by_old}="{oldv}"', f'{by_new}="{newv}"'),
            (f'BY.{by_old}("{oldv}")', f'BY.{by_new}("{newv}")'),
            (oldv, newv),
        ]
        patched = content
        for old, new in patterns:
            if old in patched:
                patched = patched.replace(old, new, 1)
                break

        if patched != content:
            with open(self.script_path, 'w', encoding='utf-8') as f:
                f.write(patched)
            print(f"  Patched: [{heal_record.get('intent', '')}] {oldv} → {newv}")
        else:
            print(f"  ! Could not match locator in script: {oldv}")


# ── HealingProxy ────────────────────────────────────────────────────


class HealingProxy:
    """
    透明代理：包装原生 driver（u2/wda），拦截元素操作并注入自愈。
    v2.0 — 多候选 + LLM 验证 + 回滚 + 因果检测。
    """

    def __init__(self, real_driver, framework='uiautomator2', script_path=None, config=None, persist_callback=None):
        self._adapter = build_adapter(real_driver, framework)
        self.framework = framework
        self.script_path = script_path
        self._agent = LLMHealingAgent(config)
        self._session = HealSession()
        self._patcher = ScriptPatcher(script_path, persist_callback) if script_path or persist_callback else None
        self._heal_records = []

    def __call__(self, **locator):
        by = list(locator.keys())[0]
        value = locator[by]
        return HealingUiObject(self._adapter.create_element(by, value), locator, self)

    def dump_hierarchy(self):
        return self._adapter.dump_hierarchy()

    def heal(self, intent, old_locator, action='click'):
        source = self.dump_hierarchy()
        if not source:
            print("  [Heal] Cannot dump hierarchy")
            return None
        return self._agent.heal(source, intent, old_locator, self.framework, action)

    def _capture_page_ctx(self):
        """采集页面三要素：DOM + 锚点 + 截图 hash"""
        return {
            'source': self.dump_hierarchy(),
            'anchor': self._adapter.get_page_anchors(),
            'screenshot_hash': self._screenshot_hash(),
        }

    def _screenshot_hash(self):
        try:
            img = self._adapter.screenshot()
            if img is not None:
                return hashlib.md5(img.tobytes()).hexdigest()
        except Exception:
            pass
        return None

    def _ctx_equal(self, a, b):
        """页面是否相同：锚点 > 截图 hash > DOM 文本"""
        if a.get('anchor') and b.get('anchor') and a['anchor'] == b['anchor']:
            return True
        if a.get('screenshot_hash') and b.get('screenshot_hash'):
            if a['screenshot_hash'] == b['screenshot_hash']:
                return True
        ratio = difflib.SequenceMatcher(
            None,
            DOMCompressor.compress(a.get('source', ''), self.framework),
            DOMCompressor.compress(b.get('source', ''), self.framework),
        ).ratio()
        return ratio >= 0.7

    def _rollback(self, target_ctx):
        """回滚到目标页面：锚点 + 截图 hash + DOM 三保险"""
        for _ in range(5):
            current = self._capture_page_ctx()
            if self._ctx_equal(current, target_ctx):
                return True
            if not current.get('anchor') or not current['anchor']:
                break
            self._adapter.press_back()
            time.sleep(0.5)

        self._adapter.restart_app()
        return False

    def _sample_dom_stable(self):
        """操作后等待动画稳定再采样"""
        time.sleep(0.5)
        dom1 = self.dump_hierarchy()
        time.sleep(0.3)
        dom2 = self.dump_hierarchy()
        if dom1 and dom2:
            ratio = difflib.SequenceMatcher(None, dom1, dom2).ratio()
            if ratio < 0.9:
                time.sleep(0.5)
                return self.dump_hierarchy()
        return dom2 or dom1

    def _write_heal(self, candidate, old_locator, intent):
        """验证通过后：记录 + 缓存 + 回写"""
        self._agent._cache.record(intent, self.framework, candidate)
        record = {
            'old_locator': old_locator,
            'new_locator': candidate,
            'intent': intent,
        }
        self._heal_records.append(record)
        if self._patcher:
            self._patcher.patch(record)
        print(f"  [Healed] [{intent}] -> {candidate['by']}=\"{candidate['value']}\"")

    def _retry_previous_operation(self):
        """因果链断裂时：回退到上一个愈合点重试"""
        if len(self._session.heal_chain) > 0:
            prev = self._session.heal_chain[-1]
            self._rollback(prev['before_ctx'])
        return None

    @property
    def heal_records(self):
        return self._heal_records

    def patch_script(self):
        pass

    def _get_min_confidence(self):
        return 0.3 if not self._agent._llm_available else 0.4


# ── Mock Driver (模拟 uiautomator2 API) ────────────────────────────


class ElementNotFound(Exception):
    pass


class MockUiObject:
    def __init__(self, elements, locator):
        self._elements = elements
        self._locator = locator

    def _find(self):
        by = list(self._locator.keys())[0]
        val = self._locator[by]
        key_map = {
            'resourceId': 'resource_id', 'text': 'text',
            'description': 'content_desc', 'className': 'class_name',
            'content-desc': 'content_desc',
        }
        for el in self._elements:
            if el.get(key_map.get(by, by)) == val:
                return el
        return None

    def click(self):
        if self._find():
            return 'clicked'
        raise ElementNotFound(f"click failed: {self._locator}")

    def tap(self):
        return self.click()

    def send_keys(self, text):
        if self._find():
            return f'sent: {text}'
        raise ElementNotFound(f"send_keys failed: {self._locator}")

    def set_text(self, text):
        return self.send_keys(text)

    def type(self, text):
        return self.send_keys(text)

    @property
    def exists(self):
        return self._find() is not None


class MockDriver:
    def __init__(self, framework='uiautomator2'):
        self.framework = framework
        self._agent = LLMHealingAgent()
        self._page_source = self._default_source()
        self._rebuild()

    def _rebuild(self):
        self._elements = self._agent._parse_hierarchy(self._page_source, self.framework)

    def __call__(self, **locator):
        return MockUiObject(self._elements, locator)

    def dump_hierarchy(self):
        return self._page_source

    def set_page_source(self, xml):
        self._page_source = xml
        self._rebuild()

    def _default_source(self):
        return '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,1920]">
    <node index="0" text="\u767b\u5f55" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" bounds="[100,800][980,960]" resource-id="com.example.app:id/sign_in_btn"/>
    <node index="1" text="\u7528\u6237\u540d" class="android.widget.EditText" package="com.example.app" content-desc="\u8f93\u5165\u7528\u6237\u540d" clickable="true" enabled="true" bounds="[100,500][980,650]" resource-id="com.example.app:id/input_username"/>
    <node index="2" text="" class="android.widget.Button" package="com.example.app" content-desc="\u63d0\u4ea4\u8868\u5355" clickable="true" enabled="true" bounds="[100,1000][980,1100]" resource-id="com.example.app:id/submit_btn"/>
  </node>
</hierarchy>'''


# ── HealingUiObject ─────────────────────────────────────────────────


class HealingUiObject:
    """透明包装 UiObject，拦截 click/tap/set_text 等操作，失败时自愈。"""

    def __init__(self, ui_object, locator, proxy):
        self._obj = ui_object
        self._locator = locator
        self._proxy = proxy

    def click(self):
        return self._do_action('click')

    def tap(self):
        return self._do_action('tap')

    def set_text(self, text):
        return self._do_action('set_text', text)

    def send_keys(self, text):
        return self._do_action('send_keys', text)

    def type(self, text):
        return self._do_action('type', text)

    @property
    def exists(self):
        return self._obj.exists if hasattr(self._obj, 'exists') else True

    def _do_action(self, action, *args):
        try:
            method = getattr(self._obj, action)
            return method(*args) if args else method()
        except Exception as e:
            print(f"  [WARN] '{action}' failed: {e}")
            return self._heal_and_retry(action, *args)

    def _heal_and_retry(self, action, *args):
        session = self._proxy._session
        intent = self._intent()

        if not session.can_attempt(intent):
            if session.consecutive_fail >= HealSession.MAX_CONSECUTIVE_FAIL:
                if self._proxy._retry_previous_operation():
                    return self._do_action(action, *args)
            raise RuntimeError(
                f"Self-heal loop detected for '{intent}': "
                f"global={session.global_count}, per-element={session.element_count.get(intent, 0)}"
            )

        session.record_attempt(intent)

        # 1. 操作前页面快照
        before_ctx = self._proxy._capture_page_ctx()

        # 2. LLM 诊断 + 候选
        heal_result = self._proxy.heal(intent, self._locator, action)
        if not heal_result or not heal_result.get('candidates'):
            session.record_fail()
            raise RuntimeError(f"Self-heal: no candidates for '{intent}'")

        candidates = [c for c in heal_result['candidates']
                      if c.get('confidence', 0) >= self._proxy._get_min_confidence()]
        if not candidates:
            session.record_fail()
            raise RuntimeError(f"Self-heal: no qualified candidates for '{intent}'")

        expected_change = heal_result.get('expected_change', '')
        expected_change_dir = heal_result.get('expected_change_dir', 'navigate')

        # 3. 逐候选尝试
        for candidate in candidates:
            new_obj = self._proxy._adapter.create_element(candidate['by'], candidate['value'])
            try:
                action_method = getattr(new_obj, action)
                result = action_method(*args) if args else action_method()
            except Exception:
                print(f"  [Heal] Candidate '{candidate['by']}={candidate['value']}' action failed")
                continue

            # 4. 等待动画稳定 + 采样操作后页面
            after_source = self._proxy._sample_dom_stable()

            # 5. 验证：有 LLM 时严格验证，无 LLM 时信任启发式直接通过
            if self._proxy._agent._llm_available:
                verify_result = self._proxy._agent.verify(
                    before_ctx.get('source', ''),
                    after_source,
                    intent, action,
                    expected_change, expected_change_dir,
                    self._proxy.framework,
                )
                verified = verify_result and verify_result.get('change_consistent') and verify_result.get('confidence', 0) >= 0.4
            else:
                verified = True  # mock 模式信任最佳匹配

            if verified:
                after_ctx = self._proxy._capture_page_ctx()
                session.record_success(before_ctx, candidate, after_ctx)
                old_loc = dict(self._locator)
                self._obj = new_obj
                self._locator = {candidate['by']: candidate['value']}
                self._proxy._write_heal(candidate, old_loc, intent)
                return result

            # 6. 验证失败 → 回滚（失败不阻断，继续下一候选）
            print(f"  [Heal] Verification failed for '{candidate['by']}={candidate['value']}', rolling back")
            try:
                self._proxy._rollback(before_ctx)
            except Exception as rb_e:
                print(f"  [Heal] Rollback failed: {rb_e}")

        # 7. 所有候选失败
        session.record_fail()
        raise RuntimeError(
            f"Self-heal failed: all candidates exhausted for '{intent}'"
        )

    def _intent(self):
        parts = [str(v) for v in self._locator.values() if v]
        return ' '.join(parts) or 'unknown'


# ── Hypium Healing Proxy ────────────────────────────────────────────


class HypiumHealingProxy:
    """Wrap hypium UiDriver, intercept touch() to self-heal on failure.
    与 HealingProxy 共享同一套 v2.0 循环逻辑。"""

    def __init__(self, real_driver, script_path=None, config=None, persist_callback=None):
        self._adapter = HypiumAdapter(real_driver)
        self.framework = 'hypium'
        self.script_path = script_path
        self._agent = LLMHealingAgent(config)
        self._session = HealSession()
        self._patcher = ScriptPatcher(script_path, persist_callback) if script_path or persist_callback else None

    def touch(self, locator):
        if isinstance(locator, tuple):
            return self._adapter.d.touch(locator)
        by, value = self._parse_by(locator)
        intent = value if value else str(locator)
        old_locator = {by: value} if by else {'unknown': str(locator)}
        try:
            # 首次尝试传原始 BY 对象（v1.0 兼容），不拆包重建
            return self._adapter.d.touch(locator)
        except Exception as e:
            print(f"  [WARN] 'touch' failed: {e}")
            return self._heal_touch(intent, old_locator)

    def _parse_by(self, locator):
        value = getattr(locator, 'match_value', None)
        source = getattr(locator, '_sourcing_call', None)
        if source and len(source) > 1:
            return source[1], value
        if value:
            return 'text', value
        s = str(locator)
        for method in ['text', 'id', 'key', 'description']:
            m = re.search(rf'{method}[\(\s=]+["\'](.+?)["\']', s)
            if m:
                return method, m.group(1)
        return None, None

    def _heal_touch(self, intent, old_locator):
        session = self._session

        if not session.can_attempt(intent):
            raise RuntimeError(f"Self-heal loop detected for '{intent}'")

        session.record_attempt(intent)
        before_ctx = self._capture_page_ctx()
        source = before_ctx.get('source', '')

        heal_result = self._agent.heal(source, intent, old_locator, 'hypium', 'touch')
        if not heal_result or not heal_result.get('candidates'):
            session.record_fail()
            raise RuntimeError(f"Self-heal: no candidates for '{intent}'")

        candidates = [c for c in heal_result['candidates']
                      if c.get('confidence', 0) >= self._min_confidence()]

        for candidate in candidates:
            try:
                new_by = self._build_by(candidate['by'], candidate['value'])
                result = self._adapter.d.touch(new_by)
            except Exception:
                continue

            after_source = self._sample_dom_stable()

            if self._agent._llm_available:
                verify_result = self._agent.verify(
                    source, after_source, intent, 'touch',
                    heal_result.get('expected_change', ''),
                    heal_result.get('expected_change_dir', 'navigate'),
                    'hypium',
                )
                verified = verify_result and verify_result.get('change_consistent') and verify_result.get('confidence', 0) >= 0.4
            else:
                verified = True

            if verified:
                after_ctx = self._capture_page_ctx()
                session.record_success(before_ctx, candidate, after_ctx)
                self._agent._cache.record(intent, 'hypium', candidate)
                if self._patcher:
                    self._patcher.patch({
                        'old_locator': old_locator,
                        'new_locator': candidate,
                        'intent': intent,
                    })
                print(f"  [Healed] [{intent}] -> {candidate['by']}=\"{candidate['value']}\"")
                return result

            self._rollback(before_ctx)

        session.record_fail()
        raise RuntimeError(f"Self-heal failed: all candidates exhausted for '{intent}'")

    def _build_by(self, by, value):
        try:
            from hypium import BY as HypiumBy
            try:
                method = getattr(HypiumBy, by)
            except AttributeError:
                return value
            if callable(method):
                return method(value)
        except ImportError:
            pass
        return value

    def _capture_page_ctx(self):
        source = self._dump_hierarchy()
        return {
            'source': source or '',
            'anchor': self._adapter.get_page_anchors(),
            'screenshot_hash': self._screenshot_hash(),
        }

    def _screenshot_hash(self):
        try:
            img = self._adapter.screenshot()
            if img is not None:
                return hashlib.md5(img.tobytes()).hexdigest()
        except Exception:
            pass
        return None

    def _sample_dom_stable(self):
        time.sleep(0.5)
        dom1 = self._dump_hierarchy()
        time.sleep(0.3)
        dom2 = self._dump_hierarchy()
        if dom1 and dom2:
            ratio = difflib.SequenceMatcher(None, dom1, dom2).ratio()
            if ratio < 0.9:
                time.sleep(0.5)
                return self._dump_hierarchy()
        return dom2 or dom1

    def _rollback(self, target_ctx):
        for _ in range(5):
            current = self._capture_page_ctx()
            if self._adapter.get_page_anchors() == target_ctx.get('anchor', {}):
                return True
            if not current.get('source'):
                break
            self._adapter.press_back()
            time.sleep(0.5)
        self._adapter.restart_app()

    def _dump_hierarchy(self):
        return self._adapter.dump_hierarchy()

    def patch_script(self):
        pass

    def __getattr__(self, name):
        return getattr(self._adapter.d, name)

    def _min_confidence(self):
        return 0.3 if not self._agent._llm_available else 0.4


# ── auto_heal 快捷入口 ──────────────────────────────────────────────


def auto_heal(driver, framework='uiautomator2', script_path=None, config=None, persist_callback=None):
    """
    一行启用自愈：
      d = auto_heal(d, framework="wda", script_path=__file__)
      d = auto_heal(d, framework="uiautomator2", script_path=__file__)
      d = auto_heal(d, framework="hypium", script_path=__file__)
    driver: u2 Session / wda Client / hypium UiDriver

    script_path: 本地 .py 文件路径（可选），愈合后原地替换定位符
    persist_callback: 自定义持久化回调（可选），收到 heal_record 后由调用方处理
                      签名: callback(heal_record: dict) -> None
    """
    if framework in ('hypium', 'harmonyos'):
        return HypiumHealingProxy(driver, script_path, config, persist_callback)
    return HealingProxy(driver, framework, script_path, config, persist_callback)