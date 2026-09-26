from playwright.sync_api import sync_playwright
URL = "http://110.40.138.167:7860/"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(4000)
    info = pg.evaluate("""() => {
      var r = {};
      // 1) URL textbox: does it have id vp_in_url?
      var url = document.getElementById('vp_in_url');
      r.urlById = !!url;
      // gradio wraps; try the wrap element
      var wrap = document.querySelector('[id="vp_in_url"]');
      r.urlWrap = !!wrap;
      // 2) nav link structure
      var navLink = document.querySelector('.vp-nav-links a');
      r.navLinkHTML = navLink ? navLink.outerHTML.slice(0,200) : 'NONE';
      // 3) all data-i18n keys in DOM now
      r.keys = Array.from(document.querySelectorAll('[data-i18n]')).map(e=>e.getAttribute('data-i18n'));
      // 4) all data-i18n-ph
      r.phKeys = Array.from(document.querySelectorAll('[data-i18n-ph]')).map(e=>e.getAttribute('data-i18n-ph'));
      // 5) the parse button text + id
      r.parseBtn = (function(){ var btns=Array.from(document.querySelectorAll('button')); var m=btns.find(x=>x.textContent.trim()==='解析视频'); if(m){ var pw=m.closest('[id]'); return {text:m.textContent, closestId: pw?pw.id:null, selfId:m.id}; } return null; })();
      // 6) gradio cache indicator: is there a data-cache or build hash?
      r.gradioVersion = (window.gradio_config && window.gradio_config.version) || 'NA';
      return r;
    }""")
    print(info)
    b.close()
