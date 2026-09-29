"""Inline docs/summary.csv and docs/manifest.json into a single-file copy of the site (for previews)."""
import sys
html=open("docs/index.html", encoding="utf-8").read(); csv=open("docs/summary.csv", encoding="utf-8").read(); man=open("docs/manifest.json", encoding="utf-8").read()
assert "</script>" not in csv
demo=(html.replace('<script id="summary-data" type="text/csv"></script>', '<script id="summary-data" type="text/csv">\n'+csv+'</script>')
          .replace('<script id="manifest-data" type="application/json"></script>', '<script id="manifest-data" type="application/json">\n'+man+'</script>'))
import os
for plat,fn in [("Twitter","words_twitter.json"),("Facebook","words_facebook.json"),("Newsletters","words_newsletters.json")]:
    path=os.path.join("docs",fn)
    if os.path.exists(path):
        demo=demo.replace(f'<script id="words-data-{plat}" type="application/json"></script>', f'<script id="words-data-{plat}" type="application/json">\n'+open(path,encoding="utf-8").read()+'</script>')
out=sys.argv[1] if len(sys.argv)>1 else "sccc-demo.html"
open(out,"w",encoding="utf-8").write(demo); print("wrote",out,len(demo)//1000,"KB")
