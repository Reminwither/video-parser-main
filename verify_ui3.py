from playwright.sync_api import sync_playwright

URL = "http://110.40.138.167:7860/"
out = {}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(3500)

    # 1) 默认（浅色）背景应为纯白
    out["lightGradBg"] = pg.evaluate("() => getComputedStyle(document.querySelector('.gradio-container')).backgroundColor")
    out["lightBodyBg"] = pg.evaluate("() => getComputedStyle(document.body).backgroundColor")

    # 2) 切换到深色，验证画布底色 = #17171d (rgb(23,23,29))
    pg.evaluate("() => { var btn=document.querySelector('button[onclick=\"vpToggleTheme()\"]'); if(btn) btn.click(); }")
    pg.wait_for_timeout(800)
    out["darkGradBg"] = pg.evaluate("() => getComputedStyle(document.querySelector('.gradio-container')).backgroundColor")
    out["darkNavBg"] = pg.evaluate("() => { var n=document.getElementById('vp-nav'); return n?getComputedStyle(n).backgroundColor:'NA'; }")
    pg.screenshot(path="shot_dark.png")
    # 切回浅色
    pg.evaluate("() => { var btn=document.querySelector('button[onclick=\"vpToggleTheme()\"]'); if(btn) btn.click(); }")
    pg.wait_for_timeout(500)

    # 3) 导航「登录」改为弹窗式：点击应打开 #vp-login-modal
    pg.click("#vp-login-link", timeout=5000)
    pg.wait_for_timeout(700)
    out["modalDisplay"] = pg.evaluate("() => { var m=document.getElementById('vp-login-modal'); return m?getComputedStyle(m).display:'NA'; }")
    pg.screenshot(path="shot_login_modal_from_nav.png")
    # 关闭
    pg.evaluate("() => { if(window.vpCloseLoginModal) window.vpCloseLoginModal(); }")
    pg.wait_for_timeout(400)

    # 4) 语言切换：点击 #vp-lang-btn -> 英文
    out["langBtnBefore"] = pg.evaluate("() => { var b=document.getElementById('vp-lang-btn'); return b?b.textContent:''; }")
    out["navParseZH"] = pg.evaluate("() => { var e=document.querySelector('[data-i18n=\"nav_parse\"]'); return e?e.textContent:''; }")
    out["heroZH"] = pg.evaluate("() => { var e=document.querySelector('[data-i18n=\"hero_sub\"]'); return e?e.textContent.slice(0,12):''; }")
    pg.click("#vp-lang-btn", timeout=5000)
    pg.wait_for_timeout(900)
    out["langBtnAfter"] = pg.evaluate("() => { var b=document.getElementById('vp-lang-btn'); return b?b.textContent:''; }")
    out["navParseEN"] = pg.evaluate("() => { var e=document.querySelector('[data-i18n=\"nav_parse\"]'); return e?e.textContent:''; }")
    out["heroEN"] = pg.evaluate("() => { var e=document.querySelector('[data-i18n=\"hero_sub\"]'); return e?e.textContent.slice(0,12):''; }")
    out["ctaEN"] = pg.evaluate("() => { var e=document.getElementById('vp_btn_parse'); return e?e.textContent:''; }")
    pg.screenshot(path="shot_en.png")
    # 切回中文
    pg.click("#vp-lang-btn", timeout=5000)
    pg.wait_for_timeout(700)
    out["navParseZH2"] = pg.evaluate("() => { var e=document.querySelector('[data-i18n=\"nav_parse\"]'); return e?e.textContent:''; }")

    b.close()

print(out)
import json
print("JSON:" + json.dumps(out, ensure_ascii=False))
