import os
import matplotlib.pyplot as plt  # type: ignore
import numpy as np  # type: ignore

# Set font family
plt.rcParams["font.sans-serif"] = ["Segoe UI", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# Create figure: Left 54% for Domain Pass Rates, Right 46% for Difficulty & Outcome Distribution
fig = plt.figure(figsize=(18, 9), dpi=300)
fig.patch.set_facecolor("#0B0F19")  # Ultra-clean Dark Palette (OpenAI/Anthropic style)

# Colors
C_MYPI = "#0EA5E9"  # Electric Sky Blue for MyPiAgent
C_PI = "#64748B"  # Steel Slate for Official Pi
C_TEXT = "#F8FAFC"  # Crisp white text
C_SUBTEXT = "#94A3B8"  # Slate grey text
C_GRID = "#1E293B"  # Subtle dark grid line
C_GOLD = "#F59E0B"  # Amber gold for win highlight
C_GREEN = "#10B981"  # Emerald green
C_RED = "#EF4444"  # Coral red for fails

gs = fig.add_gridspec(2, 2, width_ratios=[1.25, 1.0], wspace=0.26, hspace=0.38)

# ==========================================
# Subplot 1: Task Pass Rates across Domains (Left Full Height)
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

# Highlight Badge on System & DevOps (+5.9% Win)
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

leg = ax1.legend(loc="upper right", frameon=True, facecolor="#111827", edgecolor="#374151", fontsize=10)
for text in leg.get_texts():
    text.set_color(C_TEXT)

# ==========================================
# Subplot 2: Pass Rate by Difficulty Tier (Right Top)
# ==========================================
ax2 = fig.add_subplot(gs[0, 1])
ax2.set_facecolor("#0B0F19")

diff_categories = [
    "Easy (4 tasks)\nBaseline",
    "Medium (55 tasks)\nCore Challenges",
    "Hard (30 tasks)\nExtreme Engineering",
]
diff_mypi = [100.0, 83.6, 70.0]
diff_pi = [100.0, 83.6, 73.3]

x_diff = np.arange(len(diff_categories))
w_diff = 0.30

r1_d = ax2.bar(x_diff - w_diff / 2 - 0.02, diff_mypi, w_diff, label="MyPiAgent", color=C_MYPI, zorder=3, alpha=0.95)
r2_d = ax2.bar(x_diff + w_diff / 2 + 0.02, diff_pi, w_diff, label="Official Pi", color=C_PI, zorder=3, alpha=0.75)

ax2.set_ylabel("Pass Rate (%)", fontsize=11, fontweight="bold", color=C_TEXT)
ax2.set_title(
    "Pass Rate by Difficulty Tier (Official Task Metadata)", fontsize=12.5, fontweight="bold", color=C_TEXT, pad=12
)
ax2.set_xticks(x_diff)
ax2.set_xticklabels(diff_categories, fontsize=9.5, color=C_TEXT)
ax2.set_ylim(0, 118)
ax2.grid(axis="y", color=C_GRID, linestyle="--", linewidth=0.8, zorder=0)

for rect in r1_d:
    h = rect.get_height()
    ax2.annotate(
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

for rect in r2_d:
    h = rect.get_height()
    ax2.annotate(
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

for spine in ax2.spines.values():
    spine.set_color(C_GRID)
ax2.tick_params(colors=C_SUBTEXT)

# ==========================================
# Subplot 3: Head-to-Head 89 Tasks Outcome Breakdown (Right Bottom)
# ==========================================
ax3 = fig.add_subplot(gs[1, 1])
ax3.set_facecolor("#0B0F19")

outcome_labels = [
    "Double Win (65)\nBoth Solved",
    "Official Exclusive (7)\nPi Won",
    "MyPi Exclusive (6)\nMyPi Won (*)",
    "Double Fail (11)\nHardware/Assert Limit",
]
outcome_counts = [65, 7, 6, 11]
outcome_colors = [C_GREEN, C_PI, C_GOLD, C_RED]

y_pos = np.arange(len(outcome_labels))[::-1]  # Top to bottom

bars = ax3.barh(y_pos, outcome_counts, height=0.48, color=outcome_colors, zorder=3, alpha=0.90)

ax3.set_xlabel("Number of Tasks (Total: 89)", fontsize=11, fontweight="bold", color=C_TEXT)
ax3.set_title(
    "Head-to-Head Match Breakdown (89 Tasks Ground Truth)", fontsize=12.5, fontweight="bold", color=C_TEXT, pad=12
)
ax3.set_yticks(y_pos)
ax3.set_yticklabels(outcome_labels, fontsize=9.5, color=C_TEXT, fontweight="medium")
ax3.set_xlim(0, 78)
ax3.grid(axis="x", color=C_GRID, linestyle="--", linewidth=0.8, zorder=0)

for bar, count in zip(bars, outcome_counts):
    pct = count / 89 * 100
    ax3.text(
        count + 1.2,
        bar.get_y() + bar.get_height() / 2,
        f"{count} tasks ({pct:.1f}%)",
        va="center",
        fontsize=9.2,
        fontweight="bold",
        color=C_TEXT,
    )

for spine in ax3.spines.values():
    spine.set_color(C_GRID)
ax3.tick_params(colors=C_SUBTEXT)

# Global Figure Title
fig.suptitle(
    "Terminal-Bench 2.1: MyPiAgent vs Official Pi Coding Agent", fontsize=18, fontweight="bold", color=C_TEXT, y=0.975
)

plt.figtext(
    0.5,
    0.938,
    "Rigorous Head-to-Head Evaluation across 89 Tasks | Identical Host Sandbox | Base Model: DeepSeek-V4.1",
    ha="center",
    fontsize=11,
    color=C_SUBTEXT,
)

# Audited engineering footnote highlighting MyPiAgent's verified stats without unverified claims on Pi
plt.figtext(
    0.5,
    0.022,
    "MyPiAgent Audited Feature: 96.7% Prompt Cache Hit Rate (Append-Only Epochs) | 41.7M Net Billed Tokens ($4.62 USD) | Pi Official: KV-Cache unmeasured in benchmark",
    ha="center",
    fontsize=9.8,
    color=C_GOLD,
    fontweight="medium",
)

plt.subplots_adjust(top=0.865, bottom=0.10, left=0.055, right=0.96)

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
