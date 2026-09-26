from playwright.sync_api import sync_playwright
URL = "http://110.40.138.167:7860/"
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(5500)

    def txt(sel):
        return pg.evaluate(f"() => {{ var e=document.querySelector({sel!r}); return e?e.textContent.trim():'NULL'; }}")

    out["zh_parse"] = txt('#vp_btn_parse')
    out["zh_play"] = txt('#vp_btn_play')
    out["zh_url"] = txt('#vp_in_url')
    out["zh_status"] = txt('#vp_out_status')

    pg.click("#vp-lang-btn", timeout=5000)
    pg.wait_for_timeout(1300)

    out["en_parse"] = txt('#vp_btn_parse')
    out["en_play"] = txt('#vp_btn_play')
    out["en_download"] = txt('#vp_btn_download')
    out["en_extract"] = txt('#vp_btn_extract')
    out["en_clear"] = txt('#vp_btn_clear')
    out["en_url"] = txt('#vp_in_url')
    out["en_status"] = txt('#vp_out_status')
    out["en_lang"] = txt('#vp-lang-btn')
    out["en_nav"] = txt('[data-i18n="nav_parse"]')
    pg.screenshot(path="shot_en_final.png")
    b.close()
import json
print(json.dumps(out, ensure_ascii=False, indent=1))
