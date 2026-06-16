"""
Playwright probe for the Sources modal: open, render, dismiss.
Prints diagnostics (bounding box, computed transform on ancestors)
so we can see why position:fixed is being clipped.
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SHOT_DIR = Path(__file__).parent.parent / "data"
SHOT_DIR.mkdir(exist_ok=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(f"{BASE}/ui", wait_until="domcontentloaded", timeout=30_000)

        expect(page.locator("#app-header h1")).to_be_visible(timeout=15_000)
        print("✓ page loaded")

        # Modal should be hidden initially.
        n_overlay = page.locator("#sources-overlay").count()
        print(f"  #sources-overlay count: {n_overlay}")
        modal = page.locator("#sources-overlay").last
        print(f"  modal visible (initial): {modal.is_visible()}")

        # Open via header button.
        page.locator("#sources-btn button, #sources-btn").first.click()
        page.wait_for_timeout(500)
        print(f"  modal visible (after open): {modal.is_visible()}")

        # Diagnostics: bounding box + ancestor transforms.
        box = modal.bounding_box()
        print(f"  modal bbox: {box}")
        diag = page.evaluate("""
            () => {
              const all = document.querySelectorAll('#sources-overlay');
              const m = all[all.length-1];
              if (!m) return {err: 'no modal'};
              const cs = getComputedStyle(m);
              const ancestors = [];
              let n = m.parentElement;
              while (n && n !== document.body) {
                const s = getComputedStyle(n);
                if (s.transform !== 'none' || s.filter !== 'none'
                    || s.perspective !== 'none' || s.contain !== 'none'
                    || s.overflow !== 'visible') {
                  ancestors.push({
                    tag: n.tagName, cls: n.className.slice(0,80),
                    transform: s.transform, filter: s.filter,
                    contain: s.contain, overflow: s.overflow,
                  });
                }
                n = n.parentElement;
              }
              return {
                position: cs.position, top: cs.top, left: cs.left,
                transform: cs.transform, zIndex: cs.zIndex,
                width: cs.width, height: cs.height,
                maxHeight: cs.maxHeight, overflow: cs.overflow,
                offendingAncestors: ancestors,
              };
            }
        """)
        import json
        print("  computed:", json.dumps(diag, indent=2)[:2000])

        page.screenshot(path=str(SHOT_DIR / "modal_open.png"), full_page=False)
        print(f"  📸 {SHOT_DIR / 'modal_open.png'}")

        # Try to dismiss.
        close = page.locator("#sources-close button, #sources-close").first
        print(f"  close button visible: {close.is_visible()}")
        if close.is_visible():
            close.click()
            page.wait_for_timeout(400)
            print(f"  modal visible (after close): {modal.is_visible()}")
        else:
            print("  ✗ close button NOT visible — cannot dismiss")

        page.screenshot(path=str(SHOT_DIR / "modal_after_close.png"))
        browser.close()


if __name__ == "__main__":
    main()
