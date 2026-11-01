"""Verify bibliographic records against Crossref (DOI), arXiv and dblp.

Writes paper/refs_verification.json: one record per key with the metadata
returned by the authority, so every bib entry can be traced to a lookup.
"""
import json, sys, time, urllib.parse, urllib.request, re, xml.etree.ElementTree as ET
UA = {"User-Agent": "phantomdeps-cps/1.0 (reference verification)"}
def get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={**UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()
def crossref(doi):
    d = json.loads(get("https://api.crossref.org/works/" + urllib.parse.quote(doi)))["message"]
    return {"title": (d.get("title") or [""])[0], "authors": [f"{a.get('given','')} {a.get('family','')}".strip() for a in d.get("author", [])],
            "container": (d.get("container-title") or [""])[0], "year": (d.get("issued", {}).get("date-parts") or [[None]])[0][0],
            "volume": d.get("volume"), "issue": d.get("issue"), "pages": d.get("page"), "publisher": d.get("publisher"), "type": d.get("type"), "doi": d.get("DOI")}
def arxiv(aid):
    x = get("https://export.arxiv.org/api/query?id_list=" + aid, "application/atom+xml")
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    e = ET.fromstring(x).find("a:entry", ns)
    if e is None or e.find("a:title", ns) is None:
        return {"error": "not found"}
    return {"title": " ".join(e.find("a:title", ns).text.split()), "authors": [a.find("a:name", ns).text for a in e.findall("a:author", ns)],
            "published": e.find("a:published", ns).text, "updated": e.find("a:updated", ns).text,
            "journal_ref": (e.find("arxiv:journal_ref", ns).text if e.find("arxiv:journal_ref", ns) is not None else None)}
def dblp(q):
    d = json.loads(get("https://dblp.org/search/publ/api?format=json&h=5&q=" + urllib.parse.quote(q)))
    hits = d.get("result", {}).get("hits", {}).get("hit", [])
    out = []
    for h in hits:
        i = h["info"]
        au = i.get("authors", {}).get("author", [])
        au = [a["text"] for a in (au if isinstance(au, list) else [au])]
        out.append({"title": i.get("title"), "venue": i.get("venue"), "year": i.get("year"), "pages": i.get("pages"),
                    "doi": i.get("doi"), "ee": i.get("ee"), "authors": au, "key": i.get("key")})
    return out
if __name__ == "__main__":
    spec = json.load(open(sys.argv[1]))
    out = {}
    for key, s in spec.items():
        try:
            if "doi" in s: out[key] = {"via": "crossref", **crossref(s["doi"])}
            elif "arxiv" in s: out[key] = {"via": "arxiv", "id": s["arxiv"], **arxiv(s["arxiv"])}
            elif "dblp" in s: out[key] = {"via": "dblp", "query": s["dblp"], "hits": dblp(s["dblp"])}
            else: out[key] = {"via": "manual", **s}
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}", **s}
        time.sleep(0.4)
        print(key, "OK" if "error" not in out[key] else out[key]["error"], file=sys.stderr)
    json.dump(out, open(sys.argv[2], "w"), indent=1, ensure_ascii=False)
