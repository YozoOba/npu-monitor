import csv
from datetime import datetime, timedelta, timezone
import os
import tempfile
import zipfile
from xml.sax.saxutils import escape

from cluster_common.protocol import parse_timestamp


EXPORT_FIELDS = [
    'cluster_id', 'node_id', 'node_name', 'collected_at', 'received_at',
    'sample_status', 'expected_cards', 'collected_cards', 'coverage_percent',
    'card_id', 'utilization', 'hbm_used_mb', 'hbm_total_mb',
]
MAX_XLSX_ROWS = 1048576


def _parse_boundary(value, is_end=False):
    if not value:
        return None
    if len(value) == 10:
        parsed = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        return parsed + timedelta(days=1) if is_end else parsed
    return parse_timestamp(value).astimezone(timezone.utc)


def resolve_range(start=None, end=None, hours=24, max_days=31, now=None):
    now = now or datetime.now(timezone.utc)
    end_value = _parse_boundary(end, is_end=True) or now
    start_value = _parse_boundary(start) or (end_value - timedelta(hours=hours))
    if end_value <= start_value:
        raise ValueError('end must be later than start')
    if (end_value - start_value).total_seconds() > max_days * 86400:
        raise ValueError('export range cannot exceed {} days'.format(max_days))
    return start_value, end_value


def iter_export_rows(client, start, end, node_id=None, cluster_id=None,
                     card_id=None, sample_status=None, page_size=500):
    page = 1
    while True:
        result = client.samples(
            start, end, page, page_size, node_id, cluster_id, sample_status
        )
        for sample in result['items']:
            cards = sample.get('cards') or [None]
            for card in cards:
                if card_id is not None and (
                        card is None or card.get('card_id') != card_id):
                    continue
                yield {
                    'cluster_id': sample.get('cluster_id', 'default'),
                    'node_id': sample['node_id'],
                    'node_name': sample['node_name'],
                    'collected_at': sample['collected_at'],
                    'received_at': sample.get('received_at'),
                    'sample_status': sample['status'],
                    'expected_cards': sample['expected_cards'],
                    'collected_cards': sample['collected_cards'],
                    'coverage_percent': sample['coverage_percent'],
                    'card_id': None if card is None else card['card_id'],
                    'utilization': None if card is None else card['utilization'],
                    'hbm_used_mb': None if card is None else card['hbm_used_mb'],
                    'hbm_total_mb': None if card is None else card['hbm_total_mb'],
                }
        if page >= result['pages']:
            break
        page += 1


def write_csv(path, rows):
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.npu-export-', suffix='.csv.tmp', dir=os.path.dirname(path) or '.'
    )
    os.close(descriptor)
    try:
        with open(temporary_path, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            count = 0
            for row in rows:
                writer.writerow(row)
                count += 1
        os.replace(temporary_path, path)
        return count
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass


def _text(value):
    return escape(str(value), {'"': '&quot;'})


def _column_name(index):
    value = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _cell(reference, value, style=0):
    style_text = '' if not style else ' s="{}"'.format(style)
    if value is None or value == '':
        return ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="{}"{}><v>{}</v></c>'.format(reference, style_text, value)
    return '<c r="{}"{} t="inlineStr"><is><t>{}</t></is></c>'.format(
        reference, style_text, _text(value)
    )


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>'
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)
WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<bookViews><workbookView/></bookViews><sheets>'
    '<sheet name="NPU Samples" sheetId="1" r:id="rId1"/>'
    '</sheets></workbook>'
)
WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>'
)
STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/>'
    '<bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def write_xlsx(path, rows):
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.npu-export-', suffix='.xlsx.tmp', dir=os.path.dirname(path) or '.'
    )
    os.close(descriptor)
    count = 0
    try:
        with zipfile.ZipFile(
                temporary_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr('[Content_Types].xml', CONTENT_TYPES)
            archive.writestr('_rels/.rels', ROOT_RELS)
            archive.writestr('xl/workbook.xml', WORKBOOK_XML)
            archive.writestr('xl/_rels/workbook.xml.rels', WORKBOOK_RELS)
            archive.writestr('xl/styles.xml', STYLES_XML)
            with archive.open('xl/worksheets/sheet1.xml', 'w') as output:
                def write(value):
                    output.write(value.encode('utf-8'))

                write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
                write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
                write('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" '
                      'topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                      '</sheetView></sheetViews><cols>')
                widths = [16, 22, 22, 27, 27, 14, 15, 16, 17, 10, 14, 16, 16]
                for index, width in enumerate(widths, start=1):
                    write('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(
                        index, width
                    ))
                write('</cols><sheetData><row r="1" ht="22" customHeight="1">')
                for index, heading in enumerate(EXPORT_FIELDS, start=1):
                    write(_cell('{}1'.format(_column_name(index)), heading, 1))
                write('</row>')
                for row_number, row in enumerate(rows, start=2):
                    if row_number > MAX_XLSX_ROWS:
                        raise ValueError('XLSX export exceeds Excel row limit')
                    write('<row r="{}">'.format(row_number))
                    for index, field in enumerate(EXPORT_FIELDS, start=1):
                        value = row.get(field)
                        style = 2 if field in (
                            'expected_cards', 'collected_cards', 'card_id',
                            'hbm_used_mb', 'hbm_total_mb'
                        ) else 3 if field in ('coverage_percent', 'utilization') else 0
                        write(_cell(
                            '{}{}'.format(_column_name(index), row_number), value, style
                        ))
                    write('</row>')
                    count += 1
                last_row = count + 1
                write('</sheetData><autoFilter ref="A1:M{}"/></worksheet>'.format(last_row))
        os.replace(temporary_path, path)
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
    return count


def create_export(client, path, output_format, start, end, node_id=None,
                  cluster_id=None, card_id=None, sample_status=None):
    rows = iter_export_rows(
        client, start, end, node_id, cluster_id, card_id, sample_status
    )
    if output_format == 'csv':
        return write_csv(path, rows)
    if output_format == 'xlsx':
        return write_xlsx(path, rows)
    raise ValueError('format must be csv or xlsx')
