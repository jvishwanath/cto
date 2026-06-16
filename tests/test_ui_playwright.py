"""
Playwright e2e test for the Gradio UI at /ui.
Loads the page, submits a query, waits for the streamed agent response,
and verifies the trace + answer rendered in the chatbot.

Usage: python scripts/test_ui_playwright.py [URL] [QUERY]
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
QUERY = sys.argv[2] if len(sys.argv) > 2 else "where is validateCSR defined?"
SCREENSHOT = Path(__file__).parent.parent / "data" / "ui_test.png"


def main():
    print(f"URL:   {BASE}/ui")
    print(f"Query: {QUERY!r}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        page.on("console", lambda m: print(f"  [console:{m.type}] {m.text}"))
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        print("→ loading /ui ...")
        # Gradio holds a websocket open → networkidle never fires.
        page.goto(f"{BASE}/ui", wait_until="domcontentloaded", timeout=30_000)

        # Verify page loaded
        expect(page.locator("#app-header h1")).to_contain_text(
            "Code", timeout=15_000)
        expect(page.locator("#app-header h1")).to_contain_text("Origin")
        expect(page.get_by_text("From signal to source").first).to_be_visible()
        print("  ✓ page loaded, CTO header visible")

        # Sources modal: open → visible → close
        page.locator("#sources-btn button, #sources-btn").first.click()
        expect(page.locator("#sources-overlay").last).to_be_visible(timeout=5_000)
        print("  ✓ sources modal opens")
        page.locator("#sources-close button, #sources-close").first.click()
        expect(page.locator("#sources-overlay").last).not_to_be_visible(timeout=5_000)
        print("  ✓ sources modal dismisses")

        # Find the query textbox and submit
        textbox = page.get_by_placeholder("Ask about code, docs, logs, or runtime…")
        expect(textbox).to_be_visible()
        print("  ✓ query textbox found")

        print(f"\n→ submitting query ...")
        textbox.fill(QUERY)
        textbox.press("Enter")

        # Wait for the user message to appear in the chatbot
        expect(page.get_by_text(QUERY).last).to_be_visible(timeout=10_000)
        print("  ✓ user message rendered in chat")

        # Wait for router decision in the trace block
        print("  … waiting for router decision")
        expect(page.get_by_text("router →", exact=False).first).to_be_visible(timeout=30_000)
        route_el = page.get_by_text("router →", exact=False).first
        print(f"  ✓ {route_el.text_content().strip()}")

        # Wait for at least one tool call (structural query → find_symbol/find_callers)
        print("  … waiting for tool call(s)")
        try:
            page.wait_for_function(
                "() => document.body.innerText.match(/▸ (find_symbol|find_callers|search_code|grep)\\(/)",
                timeout=60_000,
            )
            print("  ✓ tool call(s) appeared in trace")
        except Exception:
            print("  ⚠ no tool calls visible (may have routed simple)")

        # Wait for iterations marker (signals agent_loop/simple_rag finished)
        print("  … waiting for completion (iterations marker)")
        page.wait_for_function(
            "() => document.body.innerText.includes('iterations:')",
            timeout=120_000,
        )
        print("  ✓ agent loop completed")

        # Wait briefly for citations to render
        time.sleep(1)

        # Capture the assistant message content
        body_text = page.evaluate("() => document.body.innerText")
        has_citations = "Citations:" in body_text or "[SOURCE_" in body_text

        # Find the last assistant bubble and extract a preview
        bot_msgs = page.locator(".message.bot, [data-testid='bot']")
        if bot_msgs.count() > 0:
            answer_text = bot_msgs.last.text_content() or ""
        else:
            # Gradio 6 may use different selectors — fall back to body slice
            answer_text = body_text

        print(f"\n--- Assistant response (first 800 chars) ---")
        print(answer_text[:800])
        print("---")

        page.screenshot(path=str(SCREENSHOT), full_page=True)
        print(f"\n  📸 screenshot → {SCREENSHOT}")

        # Assertions
        assert "router →" in body_text, "router decision not in output"
        assert "iterations:" in body_text, "no completion marker"
        print(f"\n  ✓ trace contains router + iterations")
        print(f"  {'✓' if has_citations else '⚠'} citations present: {has_citations}")

        browser.close()

    print("\n✅ UI e2e test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
