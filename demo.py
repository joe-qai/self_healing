"""
Self-Healing Demo — LLM 驱动的 APP 自动化脚本自愈

演示流程:
  1. 执行 test_script.py（含 2 个故意写错的定位符）
  2. 定位失败 → LLMHealingAgent 分析 UI hierarchy → 找到正确元素
  3. 继续执行不中断
  4. 全部完成后，回写 .py 文件替换旧定位符

使用:
  python demo.py                         # mock 模式（无需真机）
  set OPENAI_API_KEY=sk-xxx && python demo.py  # 真实 LLM 模式

  python demo.py your_script.py          # 对你自己的脚本执行自愈
"""
import sys, os

sys.path.insert(0, os.path.dirname(__file__))


def run_mock_demo():
    print("=" * 60)
    print("  Self-Healing Demo — Mock 模式 (无需真机 / 无需 API Key)")
    print("=" * 60)
    print("  mock LLM 模拟大模型推理过程，展示完整自愈+回写链路")
    print()

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "test_script", os.path.join(os.path.dirname(__file__), "test_script.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


def run_script(script_path):
    script_path = os.path.abspath(script_path)
    if not os.path.exists(script_path):
        print(f"File not found: {script_path}")
        sys.exit(1)
    print(f"Executing: {script_path}")
    spec = importlib.util.spec_from_file_location("target_script", script_path)
    mod = importlib.util.module_from_spec(spec)
    import healer
    mod.__dict__['healer'] = healer
    mod.__dict__['auto_heal'] = healer.auto_heal
    mod.__dict__['HealingProxy'] = healer.HealingProxy
    mod.__dict__['MockDriver'] = healer.MockDriver
    spec.loader.exec_module(mod)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_script(sys.argv[1])
    else:
        run_mock_demo()