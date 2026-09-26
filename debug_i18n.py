from playwright.sync_api import sync_playwright
URL = "http://110.40.138.167:7860/"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(4000)
    info = pg.evaluate("""() => {
      var r = {};
      var els = Array.from(document.querySelectorAll('[data-i18n]'));
      r.keys = els.map(function(e){ return e.getAttribute('data-i18n'); });
      r.texts = els.slice(0,6).map(function(e){ return e.tagName+':'+JSON.stringify(e.textContent); });
      // find nav_parse specifically by walking
      r.foundNavParse = els.some(function(e){ return e.getAttribute('data-i18n') === 'nav_parse'; });
      // check the nav link area
      var nav = document.getElementById('vp-login-link');
      r.navLinkHTML = nav ? nav.outerHTML.slice(0,160) : 'NO_NAV_LINK';
      return r;
    }""")
    print(info)
    b.close()
