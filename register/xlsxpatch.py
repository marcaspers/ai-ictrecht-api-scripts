"""Celwaarden in een .xlsx schrijven door alleen de XML van die cellen aan te passen.

Waarom niet openpyxl? openpyxl herschrijft bij opslaan het hele bestand en laat onderdelen
vallen die het niet kent. In het testregister zijn dat alle keuzelijsten: Excel bewaart
keuzelijsten die naar een ander tabblad verwijzen als 'x14'-extensie, en die verdwijnen.

Deze module kopieert het zip-archief onderdeel voor onderdeel en past alleen aan:
- de opgegeven cellen in de opgegeven werkbladen (de stijl van de cel blijft staan);
- ``<calcPr fullCalcOnLoad="1">`` in workbook.xml, zodat Excel bij openen herberekent.
Alle andere onderdelen worden byte voor byte overgenomen.
"""

from __future__ import annotations

import datetime as dt
import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

MAX_CELTEKST = 32767  # Excel-limiet per cel

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_ROW_RE = re.compile(r'<row\b([^>]*?)(/>|>(.*?)</row>)', re.S)
_CELL_RE = re.compile(r'<c\b([^>]*?)(/>|>(.*?)</c>)', re.S)
_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')
_REF_RE = re.compile(r'^([A-Z]+)(\d+)$')
_ONGELDIGE_XML_TEKENS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')
_EXCEL_ESCAPE = re.compile(r'_(x[0-9A-Fa-f]{4}_)')
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)


class PatchFout(RuntimeError):
    pass


def kolom_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def werkblad_paden(pad: str | Path) -> dict[str, str]:
    """Tabbladnaam -> pad van het werkblad in het zip-archief (bijv. 'xl/worksheets/sheet4.xml')."""
    with zipfile.ZipFile(pad) as z:
        return _werkblad_paden(z)


def _werkblad_paden(z: zipfile.ZipFile) -> dict[str, str]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    doelen = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", _NS)}
    paden = {}
    for sheet in wb.find("m:sheets", _NS).findall("m:sheet", _NS):
        doel = doelen[sheet.get(f"{{{_NS['r']}}}id")]
        doel = doel.lstrip("/") if doel.startswith("/") else posixpath.normpath(posixpath.join("xl", doel))
        paden[sheet.get("name")] = doel
    return paden


def _excel_tekst(tekst: str) -> str:
    tekst = _ONGELDIGE_XML_TEKENS.sub("", tekst)
    # Excel leest '_x0041_' als escape-reeks; letterlijke varianten daarom zelf escapen.
    tekst = _EXCEL_ESCAPE.sub(r'_x005F_\1', tekst)
    tekst = tekst.replace("\r\n", "\n").replace("\r", "\n")  # Excel bewaart regeleinden in cellen als \n
    return escape(tekst)


def _excel_serieel(waarde: dt.date | dt.datetime) -> float | int:
    if isinstance(waarde, dt.datetime):
        delta = waarde.replace(tzinfo=None) - _EXCEL_EPOCH
        return delta.days + delta.seconds / 86400
    return (waarde - _EXCEL_EPOCH.date()).days


def _cel_xml(ref: str, attrs: dict[str, str], waarde) -> str:
    attrs = {k: v for k, v in attrs.items() if k not in ("r", "t")}
    kop = f'<c r="{ref}"' + "".join(f' {k}="{v}"' for k, v in attrs.items())
    if waarde is None or waarde == "":
        return kop + "/>"
    if isinstance(waarde, bool):
        return kop + f' t="b"><v>{int(waarde)}</v></c>'
    if isinstance(waarde, (dt.date, dt.datetime)):
        return kop + f"><v>{_excel_serieel(waarde)}</v></c>"
    if isinstance(waarde, (int, float)):
        return kop + f"><v>{waarde!r}</v></c>"
    if isinstance(waarde, str):
        if len(waarde) > MAX_CELTEKST:
            raise PatchFout(f"{ref}: tekst van {len(waarde)} tekens past niet in een Excel-cel")
        return kop + f' t="inlineStr"><is><t xml:space="preserve">{_excel_tekst(waarde)}</t></is></c>'
    raise PatchFout(f"{ref}: waarde van type {type(waarde).__name__} niet ondersteund")


def _patch_rij(rij_xml: str, rij_nr: int, cellen: dict[str, object]) -> str:
    m = _ROW_RE.fullmatch(rij_xml)
    rij_attrs, inhoud = m.group(1), m.group(3) or ""
    nog_te_doen = dict(cellen)

    def vervang(cm: re.Match) -> str:
        attrs = dict(_ATTR_RE.findall(cm.group(1)))
        ref = attrs.get("r")
        if ref not in nog_te_doen:
            return cm.group(0)
        if "<f>" in (cm.group(3) or "") or "<f " in (cm.group(3) or ""):
            raise PatchFout(f"{ref} bevat een formule en wordt niet overschreven")
        return _cel_xml(ref, attrs, nog_te_doen.pop(ref))

    inhoud = _CELL_RE.sub(vervang, inhoud)

    # Cellen die nog niet als element bestaan op de juiste kolompositie invoegen.
    for ref, waarde in sorted(nog_te_doen.items(), key=lambda kv: kolom_index(_REF_RE.match(kv[0]).group(1))):
        nieuw = _cel_xml(ref, {}, waarde)
        doel = kolom_index(_REF_RE.match(ref).group(1))
        positie = len(inhoud)
        for cm in _CELL_RE.finditer(inhoud):
            r = dict(_ATTR_RE.findall(cm.group(1))).get("r", "")
            rm = _REF_RE.match(r)
            if rm and kolom_index(rm.group(1)) > doel:
                positie = cm.start()
                break
        inhoud = inhoud[:positie] + nieuw + inhoud[positie:]
    return f"<row{rij_attrs}>{inhoud}</row>"


def patch_werkblad(xml: str, cellen: dict[str, object]) -> str:
    """Zet waarden in een werkblad-XML. ``cellen``: {'A5': 'tekst', 'I5': 2, ...}."""
    per_rij: dict[int, dict[str, object]] = {}
    for ref, waarde in cellen.items():
        m = _REF_RE.match(ref)
        if not m:
            raise PatchFout(f"Ongeldige celverwijzing: {ref}")
        per_rij.setdefault(int(m.group(2)), {})[ref] = waarde

    start = xml.index("<sheetData")
    eind = xml.index("</sheetData>") if "</sheetData>" in xml else None
    if eind is None:
        raise PatchFout("Werkblad heeft geen rijen")
    data = xml[start:eind]
    gevonden = set()

    def vervang(rm: re.Match) -> str:
        nr = int(dict(_ATTR_RE.findall(rm.group(1)))["r"])
        if nr not in per_rij:
            return rm.group(0)
        gevonden.add(nr)
        return _patch_rij(rm.group(0), nr, per_rij[nr])

    data = _ROW_RE.sub(vervang, data)
    ontbrekend = sorted(set(per_rij) - gevonden)
    if ontbrekend:
        raise PatchFout(f"Rij(en) {ontbrekend} bestaan niet in het werkblad; er worden geen rijen toegevoegd")
    return xml[:start] + data + xml[eind:]


def zet_volledige_herberekening(xml: str) -> str:
    m = re.search(r'<calcPr\b([^>]*?)(/?)>', xml)
    if m:
        attrs = m.group(1)
        if "fullCalcOnLoad=" in attrs:
            attrs = re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', attrs)
        else:
            attrs += ' fullCalcOnLoad="1"'
        return xml[:m.start()] + f"<calcPr{attrs}{m.group(2)}>" + xml[m.end():]
    # Volgens het schema komt calcPr na definedNames/externalReferences/functionGroups/sheets.
    for tag in ("</definedNames>", "</externalReferences>", "</functionGroups>", "</sheets>"):
        if tag in xml:
            i = xml.index(tag) + len(tag)
            return xml[:i] + '<calcPr fullCalcOnLoad="1"/>' + xml[i:]
    raise PatchFout("workbook.xml heeft geen <sheets>")


def patch_werkmap(bron: str | Path, doel: str | Path, wijzigingen: dict[str, dict[str, object]]) -> Path:
    """Schrijf een kopie van ``bron`` naar ``doel`` met de gegeven celwaarden.

    ``wijzigingen``: {tabbladnaam: {'A5': waarde, ...}}. ``doel`` mag nog niet bestaan.
    """
    bron, doel = Path(bron), Path(doel)
    if doel.resolve() == bron.resolve():
        raise PatchFout("Het invoerbestand wordt nooit overschreven")
    if doel.exists():
        raise PatchFout(f"{doel} bestaat al; er wordt niets overschreven")

    with zipfile.ZipFile(bron) as zin:
        paden = _werkblad_paden(zin)
        onbekend = set(wijzigingen) - set(paden)
        if onbekend:
            raise PatchFout(f"Onbekende tabbladen: {sorted(onbekend)}")
        nieuw: dict[str, bytes] = {}
        for blad, cellen in wijzigingen.items():
            if cellen:
                xml = zin.read(paden[blad]).decode("utf-8")
                nieuw[paden[blad]] = patch_werkblad(xml, cellen).encode("utf-8")
        nieuw["xl/workbook.xml"] = zet_volledige_herberekening(zin.read("xl/workbook.xml").decode("utf-8")).encode("utf-8")

        doel.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=doel.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w") as zuit:
                for info in zin.infolist():
                    zuit.writestr(info, nieuw.get(info.filename, zin.read(info.filename)))
            if doel.exists():
                raise PatchFout(f"{doel} bestaat al; er wordt niets overschreven")
            os.replace(tmp, doel)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return doel
