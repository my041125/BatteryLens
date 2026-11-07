"""统一测试入口：串行跑 tests/ 下全部用例（每个测试独立子进程，互不污染）。

用法: python tests\\run_all.py
需要与运行程序相同的 Python 环境（依赖见 requirements.txt）。
全部用例为合成数据 + 打桩，无窗口、不占托盘、不写真实 settings.json。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
TESTS = [
    "test_render.py",       # 渲染冒烟 + 设置兼容 + 贴片/主图标离屏
    "test_features.py",     # 续航估算 + 充电管家状态机
    "test_toggle.py",       # 显隐状态机（auto 主题/忙碌/强制显示）
    "test_showdesktop.py",  # 显示桌面韧性（自愈/最小化/忙碌守卫）
    "test_summon.py",       # 兜底召唤（贴片+忙碌+隐藏最恶劣组合）
]


def main():
    failed = []
    for t in TESTS:
        r = subprocess.run([sys.executable, os.path.join(HERE, t)],
                           cwd=PROJECT)
        status = "PASS" if r.returncode == 0 else "FAIL"
        print("\n== %s -> %s ==\n" % (t, status))
        if r.returncode != 0:
            failed.append(t)
    print("RESULT:", "ALL PASS" if not failed else "FAIL: %s" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
