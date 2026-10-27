"""PNG charts with correct Persian (RTL) text rendering."""
import io
import os

import matplotlib

matplotlib.use("Agg")
import arabic_reshaper  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from bidi.algorithm import get_display  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from .config import config  # noqa: E402
from .utils import fa  # noqa: E402

_FONT_CANDIDATES = [
    config.font_path,
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
for _path in _FONT_CANDIDATES:
    if _path and os.path.exists(_path):
        font_manager.fontManager.addfont(_path)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=_path).get_name()
        break

PALETTE = ["#2a9d8f", "#e76f51", "#f4a261", "#264653", "#e9c46a", "#8ab17d",
           "#b56576", "#6d597a", "#457b9d", "#a8dadc"]


# matplotlib >= 3.11 shapes and orders RTL text itself; older versions need manual reshaping.
_NATIVE_RTL = tuple(int(p) for p in matplotlib.__version__.split(".")[:2]) >= (3, 11)


def rtl(text: str) -> str:
    return str(text) if _NATIVE_RTL else get_display(arabic_reshaper.reshape(str(text)))


def _png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _short(n: int) -> str:
    if abs(n) >= 1_000_000:
        return fa(f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")).replace(".", "٫") + " میلیون"
    if abs(n) >= 1_000:
        return fa(f"{n / 1_000:.0f}") + " هزار"
    return fa(n)


def category_pie(by_category: dict[str, int], title: str) -> io.BytesIO | None:
    if not by_category:
        return None
    items = list(by_category.items())
    if len(items) > 8:  # keep the chart readable
        items = items[:7] + [("سایر موارد", sum(v for _, v in items[7:]))]
    labels = [rtl(k) for k, _ in items]
    values = [v for _, v in items]
    total = sum(values)
    fig, ax = plt.subplots(figsize=(7, 5.5))
    wedges, _ = ax.pie(values, colors=PALETTE[: len(values)], startangle=90,
                       wedgeprops={"width": 0.45, "edgecolor": "white"})
    ax.legend(wedges, [f"{lbl}  {rtl(fa(round(v * 100 / total)) + '٪')}" for lbl, v in zip(labels, values)],
              loc="center left", bbox_to_anchor=(1, 0.5), frameon=False, fontsize=11)
    ax.text(0, 0, rtl(_short(total)), ha="center", va="center", fontsize=15, fontweight="bold")
    ax.set_title(rtl(title), fontsize=14)
    return _png(fig)


def trend_bars(trend: list[dict], title: str = "روند درآمد و هزینه ماهانه") -> io.BytesIO | None:
    if not any(t["income"] or t["expense"] for t in trend):
        return None
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = range(len(trend))
    w = 0.38
    inc = ax.bar([i - w / 2 for i in x], [t["income"] for t in trend], w, color="#2a9d8f", label=rtl("درآمد"))
    exp = ax.bar([i + w / 2 for i in x], [t["expense"] for t in trend], w, color="#e76f51", label=rtl("هزینه"))
    for bars in (inc, exp):
        for b in bars:
            if b.get_height():
                ax.annotate(rtl(_short(int(b.get_height()))), (b.get_x() + b.get_width() / 2, b.get_height()),
                            ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(x), [rtl(t["label"]) for t in trend], fontsize=9)
    ax.yaxis.set_major_formatter(lambda v, _: rtl(_short(int(v))))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False)
    ax.set_title(rtl(title), fontsize=14)
    return _png(fig)
