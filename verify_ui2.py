from playwright.sync_api import sync_playwright

URL = "http://110.40.138.167:7860/"
out = {}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # ---- 1) 深色画布 = #17171d ----
    pg.evaluate("localStorage.setItem('vp-theme-v2','dark')")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    out["darkBodyBg"] = pg.evaluate("getComputedStyle(document.body).backgroundColor")

    # 回浅色做其余测试
    pg.evaluate("localStorage.setItem('vp-theme-v2','light')")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # ---- 2) 导航登录 -> 弹窗 ----
    out["modalBefore"] = pg.evaluate("getComputedStyle(document.getElementById('vp-login-modal')).display")
    pg.click(".vp-login-link")
    pg.wait_for_timeout(500)
    out["modalAfter"] = pg.evaluate("getComputedStyle(document.getElementById('vp-login-modal')).display")
    pg.screenshot(path="shot_login_modal_from_nav.png")

    # 关弹窗
    pg.evaluate("window.vpCloseLoginModal && window.vpCloseLoginModal()")
    pg.wait_for_timeout(300)

    # ---- 3) 语言切换 ----
    out["navParseZh"] = pg.evaluate("document.querySelector('[data-i18n=nav_parse]').textContent")
    pg.click("#vp-lang-btn")
    pg.wait_for_timeout(600)
    out["navParseEn"] = pg.evaluate("document.querySelector('[data-i18n=nav_parse]').textContent")
    out["heroTitleBEn"] = pg.evaluate("document.querySelector('[data-i18n=hero_title_b]').textContent")
    out["langBtnLabel"] = pg.evaluate("document.getElementById('vp-lang-btn').textContent")
    out["parseBtnLabelEn"] = pg.evaluate(
        "var r=document.getElementById('vp_btn_parse');"
        "return r && r.querySelector('button') ? r.querySelector('button').textContent : 'NO_EL';"
    )
    pg.screenshot(path="shot_lang_en.png")
    # 切回中文
    pg.click("#vp-lang-btn")
    pg.wait_for_timeout(400)
    out["navParseBackZh"] = pg.evaluate("document.querySelector('[data-i18n=nav_parse]').textContent")
    pg.screenshot(path="shot_lang_zh.png")

    b.close()

print(out)
