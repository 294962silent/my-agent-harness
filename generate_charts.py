import os
import matplotlib.pyplot as plt  # type: ignore
import numpy as np  # type: ignore

# Set font family
plt.rcParams["font.sans-serif"] = ["Segoe UI", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# Create figure with 2 main columns (wider landscape for zero-overlap perfection)
fig = plt.figure(figsize=(18, 9), dpi=300)
fig.patch.set_facecolor("#0B0F19")  # Ultra-clean Dark Palette (OpenAI/Anthropic style)

# Colors
C_MYPI = "#0EA5E9"  # Electric Sky Blue for MyPiAgent
C_PI = "#64748B"  # Steel Slate for Official Pi
C_TEXT = "#F8FAFC"  # Crisp white text
C_SUBTEXT = "#94A3B8"  # Slate grey text
C_GRID = "#1E293B"  # Subtle dark grid line
C_ACCENT = "#10B981"  # Emerald green accent for savings/highlights
C_GOLD = "#F59E0B"  # Amber gold for win highlight

# Grid layout: left column takes 54% width, right column takes 46% width
gs = fig.add_gridspec(3, 2, width_ratios=[1.22, 1.0], wspace=0.26, hspace=0.48)

# ==========================================
# Subplot 1: Task Pass Rates across Domains
# ==========================================
ax1 = fig.add_subplot(gs[:, 0])
ax1.set_facecolor("#0B0F19")

categories = [
    "Valid Pass Rate\n(85 tasks)",
    "Full Pass Rate\n(89 tasks)",
    "System & DevOps\n(17 tasks)",
    "AI & Data Science\n(20 tasks)",
    "Math & Science\n(13 tasks)",
    "Software Eng\n(36 tasks)",
]

mypi_acc = [83.5, 79.8, 76.5, 80.0, 76.9, 83.3]
pi_acc = [84.7, 80.9, 70.6, 80.0, 76.9, 86.1]

x = np.arange(len(categories))
width = 0.32

rects1 = ax1.bar(
    x - width / 2 - 0.02,
    mypi_acc,
    width,
    label="MyPiAgent (Ours)",
    color=C_MYPI,
    edgecolor="none",
    zorder=3,
    alpha=0.95,
)
rects2 = ax1.bar(
    x + width / 2 + 0.02, pi_acc, width, label="Official Pi Agent", color=C_PI, edgecolor="none", zorder=3, alpha=0.75
)

ax1.set_ylabel("Pass Rate (%)", fontsize=12, fontweight="bold", color=C_TEXT)
ax1.set_title("Task Pass Rates by Domain & Scope", fontsize=14, fontweight="bold", color=C_TEXT, pad=14)
ax1.set_xticks(x)
ax1.set_xticklabels(categories, fontsize=9.5, color=C_TEXT, fontweight="medium")
ax1.set_ylim(0, 115)
ax1.grid(axis="y", color=C_GRID, linestyle="--", linewidth=0.8, zorder=0)

# Custom data labels with slight horizontal offsets to eliminate collisions
for rect in rects1:
    h = rect.get_height()
    ax1.annotate(
        f"{h:.1f}%",
        xy=(rect.get_x() + rect.get_width() / 2, h),
        xytext=(-1, 4),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=8.8,
        fontweight="bold",
        color=C_MYPI,
    )

for rect in rects2:
    h = rect.get_height()
    ax1.annotate(
        f"{h:.1f}%",
        xy=(rect.get_x() + rect.get_width() / 2, h),
        xytext=(1, 4),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=8.8,
        fontweight="normal",
        color=C_TEXT,
    )

# Highlight Badge on System & DevOps (+5.9% Win) with rounded pill box, clear of the text
ax1.annotate(
    "+5.9% Win",
    xy=(2 - width / 2 - 0.02, 81.5),
    xytext=(2 - width / 2 - 0.02, 95),
    ha="center",
    va="bottom",
    fontsize=9.5,
    fontweight="bold",
    color=C_GOLD,
    bbox=dict(boxstyle="round,pad=0.35", fc="#111827", ec=C_GOLD, lw=1.2),
    arrowprops=dict(arrowstyle="->", color=C_GOLD, lw=1.3, shrinkB=2),
)

for spine in ax1.spines.values():
    spine.set_color(C_GRID)
ax1.tick_params(colors=C_SUBTEXT)

# Legend on Left Subplot
leg = ax1.legend(loc="upper right", frameon=True, facecolor="#111827", edgecolor="#374151", fontsize=10)
for text in leg.get_texts():
    text.set_color(C_TEXT)

# ==========================================
# Subplots 2, 3, 4: Efficiency & Economics
# ==========================================
ax2_1 = fig.add_subplot(gs[0, 1])
ax2_2 = fig.add_subplot(gs[1, 1])
ax2_3 = fig.add_subplot(gs[2, 1])

for ax in [ax2_1, ax2_2, ax2_3]:
    ax.set_facecolor("#0B0F19")
    ax.grid(axis="x", color=C_GRID, linestyle="--", linewidth=0.8, zorder=0)
    for spine in ax.spines.values():
        spine.set_color(C_GRID)
    ax.tick_params(colors=C_SUBTEXT)

# 1. Total Tokens (Millions)
m_tokens_my = 41.7
m_tokens_pi = 93.3
ax2_1.barh([1], [m_tokens_my], height=0.40, color=C_MYPI, alpha=0.95, zorder=3)
ax2_1.barh([0], [m_tokens_pi], height=0.40, color=C_PI, alpha=0.75, zorder=3)
ax2_1.set_yticks([1, 0])
ax2_1.set_yticklabels(["MyPiAgent", "Official Pi"], fontsize=10, color=C_TEXT, fontweight="bold")
ax2_1.set_title(
    r"Total Tokens Consumed (Millions)  ▼ 55.3% Token Savings",
    fontsize=11.5,
    fontweight="bold",
    color=C_ACCENT,
    pad=8,
    loc="left",
)
ax2_1.set_xlim(0, 125)
ax2_1.text(m_tokens_my + 2.5, 1, "41.7 M (-55.3%)", va="center", fontsize=9.5, fontweight="bold", color=C_MYPI)
ax2_1.text(m_tokens_pi + 2.5, 0, "93.3 M", va="center", fontsize=9.5, color=C_SUBTEXT)

# 2. Prompt Cache Hit Rate (%)
m_cache_my = 96.7
m_cache_pi = 35.0
ax2_2.barh([1], [m_cache_my], height=0.40, color=C_MYPI, alpha=0.95, zorder=3)
ax2_2.barh([0], [m_cache_pi], height=0.40, color=C_PI, alpha=0.75, zorder=3)
ax2_2.set_yticks([1, 0])
ax2_2.set_yticklabels(["MyPiAgent", "Official Pi"], fontsize=10, color=C_TEXT, fontweight="bold")
ax2_2.set_title(
    r"Prompt Cache Hit Rate (%)  ▲ 2.8x Higher Prefix Cache",
    fontsize=11.5,
    fontweight="bold",
    color=C_ACCENT,
    pad=8,
    loc="left",
)
ax2_2.set_xlim(0, 125)
ax2_2.text(m_cache_my + 2.5, 1, "96.7% (Append-Only Epoch)", va="center", fontsize=9.5, fontweight="bold", color=C_MYPI)
ax2_2.text(m_cache_pi + 2.5, 0, "~35.0% (Rolling Pruned)", va="center", fontsize=9.5, color=C_SUBTEXT)

# 3. Total Benchmark Cost ($ USD) - Escaping dollar signs to avoid MathJax math mode
m_cost_my = 4.62
m_cost_pi = 10.50
ax2_3.barh([1], [m_cost_my], height=0.40, color=C_MYPI, alpha=0.95, zorder=3)
ax2_3.barh([0], [m_cost_pi], height=0.40, color=C_PI, alpha=0.75, zorder=3)
ax2_3.set_yticks([1, 0])
ax2_3.set_yticklabels(["MyPiAgent", "Official Pi"], fontsize=10, color=C_TEXT, fontweight="bold")
ax2_3.set_title(
    r"Total Benchmark Cost (USD)  ▼ 56.0% Lower Cost",
    fontsize=11.5,
    fontweight="bold",
    color=C_ACCENT,
    pad=8,
    loc="left",
)
ax2_3.set_xlim(0, 15)
ax2_3.text(m_cost_my + 0.35, 1, r"\$4.62 (Save \$5.88)", va="center", fontsize=9.5, fontweight="bold", color=C_MYPI)
ax2_3.text(m_cost_pi + 0.35, 0, r"\$10.50", va="center", fontsize=9.5, color=C_SUBTEXT)

# Global Figure Title
fig.suptitle(
    "Terminal-Bench 2.1: MyPiAgent vs Official Pi Coding Agent", fontsize=18, fontweight="bold", color=C_TEXT, y=0.975
)

plt.figtext(
    0.5,
    0.935,
    "Evaluated on 89 Linux system, DevOps, compilation & reverse-engineering tasks | Base Model: DeepSeek-V4.1",
    ha="center",
    fontsize=11,
    color=C_SUBTEXT,
)

# Keep spacious top margin so titles and subtitles never collide
plt.subplots_adjust(top=0.865, bottom=0.08, left=0.055, right=0.96)

out_paths = [
    "docs/assets/terminal_bench_comparison.png",
    "my-pi-eval/results/terminal_bench_comparison.png",
]

blog_asset_dir = "D:/blog/source/_posts/学习/agent实战/my-pi-agent/my-pi-agent--agent评估"
try:
    if os.path.exists(os.path.dirname(blog_asset_dir)):
        os.makedirs(blog_asset_dir, exist_ok=True)
        out_paths.append(os.path.join(blog_asset_dir, "terminal_bench_comparison.png"))
except OSError as e:
    print(f"Warning creating blog asset dir: {e}")

for p in out_paths:
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fig.savefig(p, dpi=300, bbox_inches="tight", facecolor=fig.get_facecolor())
        print(f"Successfully generated: {p}")
    except OSError as e:
        print(f"Failed to write {p}: {e}")
