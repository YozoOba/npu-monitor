import csv
from datetime import datetime, timedelta
import logging
import math
import os
import re
import tempfile
import zipfile
from xml.sax.saxutils import escape, quoteattr

from cluster_common.protocol import parse_timestamp
from cluster_common.timezones import (
    resolve_timezone, timezone_label, timezone_offset_seconds,
)


CSV_FIELDS = ['timestamp', 'card_id', 'utilization', 'hbm_used_mb', 'hbm_total_mb']
DAILY_FILE_PATTERN = re.compile(r'^stats_(\d{4}-\d{2}-\d{2})\.csv$')
MONTHLY_FILE_PATTERN = re.compile(r'^.+_(\d{4}-\d{2})\.xlsx$')
LOGGER = logging.getLogger('npu_agent.monthly_xlsx')


class MonthlyWorkbookError(ValueError):
    pass


def _xml_text(value):
    return escape(str(value), {'\"': '&quot;'})


def _inline_cell(reference, value, style=None):
    style_text = '' if style is None else ' s="{}"'.format(style)
    return (
        '<c r="{}"{} t="inlineStr"><is><t>{}</t></is></c>'.format(
            reference, style_text, _xml_text(value)
        )
    )


def _number_cell(reference, value, style=None):
    style_text = '' if style is None else ' s="{}"'.format(style)
    return '<c r="{}"{}><v>{}</v></c>'.format(reference, style_text, value)


def _read_day_rows(path, local_timezone=None):
    local_timezone = local_timezone or resolve_timezone('auto')
    seen = set()
    with open(path, 'r', encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_FIELDS:
            raise MonthlyWorkbookError(
                '{} has unexpected CSV fields: {!r}'.format(path, reader.fieldnames)
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                source_timestamp = row['timestamp'].strip()
                local_collected = parse_timestamp(source_timestamp).astimezone(
                    local_timezone
                )
                timestamp = local_collected.isoformat(timespec='seconds')
                card_id = int(row['card_id'])
                utilization = float(row['utilization'])
                hbm_used = row['hbm_used_mb'].strip()
                hbm_total = row['hbm_total_mb'].strip()
                hbm_used_value = None if hbm_used == '' else float(hbm_used)
                hbm_total_value = None if hbm_total == '' else float(hbm_total)
            except (AttributeError, TypeError, ValueError) as exc:
                raise MonthlyWorkbookError(
                    '{} line {} is invalid: {}'.format(path, line_number, exc)
                )
            if not source_timestamp:
                raise MonthlyWorkbookError(
                    '{} line {} has an empty timestamp'.format(path, line_number)
                )
            if card_id < 0:
                raise MonthlyWorkbookError(
                    '{} line {} has a negative card_id'.format(path, line_number)
                )
            if not math.isfinite(utilization) or not 0 <= utilization <= 100:
                raise MonthlyWorkbookError(
                    '{} line {} has invalid utilization'.format(path, line_number)
                )
            for label, value in (
                    ('hbm_used_mb', hbm_used_value),
                    ('hbm_total_mb', hbm_total_value)):
                if value is not None and (not math.isfinite(value) or value < 0):
                    raise MonthlyWorkbookError(
                        '{} line {} has invalid {}'.format(path, line_number, label)
                    )
            if (hbm_used_value is not None and hbm_total_value is not None and
                    hbm_used_value > hbm_total_value):
                raise MonthlyWorkbookError(
                    '{} line {} has HBM usage greater than capacity'.format(
                        path, line_number
                    )
                )
            identity = (int(local_collected.timestamp()), card_id)
            if identity in seen:
                continue
            seen.add(identity)
            yield (
                local_collected.date(), timestamp, card_id, utilization,
                hbm_used_value, hbm_total_value
            )


def _discover_daily_files(daily_dir, through_date, local_timezone=None):
    local_timezone = local_timezone or resolve_timezone('auto')
    paths_by_date = {}
    try:
        names = os.listdir(daily_dir)
    except OSError:
        return {}
    for name in names:
        match = DAILY_FILE_PATTERN.fullmatch(name)
        if not match:
            continue
        path = os.path.join(daily_dir, name)
        for file_date, _timestamp, _card_id, _utilization, _used, _total in (
                _read_day_rows(path, local_timezone)):
            if file_date <= through_date:
                paths_by_date.setdefault(file_date, set()).add(path)
    months = {}
    for file_date, paths in paths_by_date.items():
        month = file_date.strftime('%Y-%m')
        months.setdefault(month, []).append((file_date, sorted(paths)))
    for month_values in months.values():
        month_values.sort(key=lambda value: value[0])
    return months


def _write_sheet(
        archive, sheet_index, sheet_date, csv_paths, node_info,
        local_timezone):
    with archive.open('xl/worksheets/sheet{}.xml'.format(sheet_index), 'w') as output:
        def write(value):
            output.write(value.encode('utf-8'))

        write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
        write('<sheetViews><sheetView workbookViewId="0">')
        write('<pane ySplit="7" topLeftCell="A8" activePane="bottomLeft" state="frozen"/>')
        write('</sheetView></sheetViews>')
        write('<cols>')
        write('<col min="1" max="1" width="28" customWidth="1"/>')
        write('<col min="2" max="2" width="10" customWidth="1"/>')
        write('<col min="3" max="5" width="18" customWidth="1"/>')
        write('</cols><sheetData>')
        local_timezone_name = timezone_label(
            timezone_offset_seconds(local_timezone)
        )
        metadata = (
            ('node_name', node_info['node_name']),
            ('node_id', node_info['node_id']),
            ('cluster_id', node_info['cluster_id']),
            ('date', sheet_date.isoformat()),
            ('timezone', local_timezone_name),
        )
        for row_index, (label, value) in enumerate(metadata, start=1):
            write('<row r="{}">'.format(row_index))
            write(_inline_cell('A{}'.format(row_index), label, style=1))
            write(_inline_cell('B{}'.format(row_index), value))
            write('</row>')
        write('<row r="7" ht="22" customHeight="1">')
        headings = (
            'timestamp_{}'.format(local_timezone_name.lower()),
            'card_id', 'utilization',
            'hbm_used_mb', 'hbm_total_mb',
        )
        for column, heading in zip('ABCDE', headings):
            write(_inline_cell('{}7'.format(column), heading, style=1))
        write('</row>')

        row_number = 7
        seen = set()
        rows = (
            values for csv_path in (csv_paths or ())
            for values in _read_day_rows(csv_path, local_timezone)
            if values[0] == sheet_date
        )
        for values in rows:
            _local_date, timestamp, card_id, utilization, hbm_used, hbm_total = values
            identity = (timestamp, card_id)
            if identity in seen:
                continue
            seen.add(identity)
            row_number += 1
            write('<row r="{}">'.format(row_number))
            write(_inline_cell('A{}'.format(row_number), timestamp))
            write(_number_cell('B{}'.format(row_number), card_id, style=2))
            write(_number_cell('C{}'.format(row_number), utilization, style=3))
            if hbm_used is not None:
                write(_number_cell('D{}'.format(row_number), hbm_used, style=3))
            if hbm_total is not None:
                write(_number_cell('E{}'.format(row_number), hbm_total, style=3))
            write('</row>')
        write('</sheetData>')
        write('<autoFilter ref="A7:E{}"/>'.format(row_number))
        write('</worksheet>')


def _content_types(sheet_count):
    sheet_types = ''.join(
        '<Override PartName="/xl/worksheets/sheet{}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        .format(index)
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '{}'
        '</Types>'
    ).format(sheet_types)


def _workbook_xml(day_files):
    sheets = ''.join(
        '<sheet name={} sheetId="{}" r:id="rId{}"/>'.format(
            quoteattr(file_date.isoformat()), index, index
        )
        for index, (file_date, _path) in enumerate(day_files, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView/></bookViews><sheets>{}</sheets></workbook>'
    ).format(sheets)


def _workbook_relationships(sheet_count):
    relationships = ''.join(
        '<Relationship Id="rId{}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet{}.xml"/>'.format(index, index)
        for index in range(1, sheet_count + 1)
    )
    relationships += (
        '<Relationship Id="rId{}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'.format(sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '{}</Relationships>'
    ).format(relationships)


ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
    'Target="docProps/app.xml"/>'
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
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    '<Application>NPU Monitor</Application></Properties>'
)


CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:creator>NPU Monitor</dc:creator><cp:lastModifiedBy>NPU Monitor</cp:lastModifiedBy>'
    '</cp:coreProperties>'
)


def build_monthly_workbook(
        day_files, output_path, node_info=None, local_timezone=None):
    if not day_files:
        return None
    day_files = [
        (file_date, [paths] if isinstance(paths, str) else paths)
        for file_date, paths in day_files
    ]
    node_info = node_info or {
        'node_id': 'unknown', 'node_name': 'unknown', 'cluster_id': 'default'
    }
    local_timezone = local_timezone or resolve_timezone('auto')
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.monthly-', suffix='.xlsx.tmp', dir=output_dir
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
                temporary_path, 'w', compression=zipfile.ZIP_DEFLATED,
                allowZip64=True) as archive:
            archive.writestr('[Content_Types].xml', _content_types(len(day_files)))
            archive.writestr('_rels/.rels', ROOT_RELS)
            archive.writestr('docProps/app.xml', APP_XML)
            archive.writestr('docProps/core.xml', CORE_XML)
            archive.writestr('xl/workbook.xml', _workbook_xml(day_files))
            archive.writestr(
                'xl/_rels/workbook.xml.rels',
                _workbook_relationships(len(day_files)),
            )
            archive.writestr('xl/styles.xml', STYLES_XML)
            for sheet_index, (file_date, csv_paths) in enumerate(day_files, start=1):
                _write_sheet(
                    archive, sheet_index, file_date, csv_paths, node_info,
                    local_timezone,
                )
        with open(temporary_path, 'rb+') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
        return output_path
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass


def _safe_filename(value):
    safe = ''.join(
        character if character.isalnum() or character in ('-', '_', '.') else '_'
        for character in str(value).strip()
    ).strip('._')
    safe = re.sub(r'_+', '_', safe)
    return (safe or 'npu-node')[:100]


def update_monthly_workbooks(
        daily_dir, monthly_dir, through_date, node_info=None,
        local_timezone=None):
    node_info = node_info or {
        'node_id': 'unknown', 'node_name': 'unknown', 'cluster_id': 'default'
    }
    local_timezone = local_timezone or resolve_timezone('auto')
    months = _discover_daily_files(
        daily_dir, through_date, local_timezone
    )
    updated = []
    active_month = through_date.strftime('%Y-%m')
    for month in sorted(months):
        output_path = os.path.join(
            monthly_dir,
            '{}_{}.xlsx'.format(_safe_filename(node_info['node_name']), month),
        )
        if month != active_month and os.path.exists(output_path):
            continue
        paths_by_date = dict(months[month])
        first_date = months[month][0][0]
        if month == active_month:
            last_date = through_date
        else:
            month_start = datetime.strptime(month, '%Y-%m').date()
            if month_start.month == 12:
                next_month = month_start.replace(
                    year=month_start.year + 1, month=1
                )
            else:
                next_month = month_start.replace(month=month_start.month + 1)
            last_date = next_month - timedelta(days=1)
        day_files = []
        current_date = first_date
        while current_date <= last_date:
            day_files.append((current_date, paths_by_date.get(current_date)))
            current_date += timedelta(days=1)
        result = build_monthly_workbook(
            day_files, output_path, node_info, local_timezone
        )
        if result:
            updated.append(result)
    return updated


def archive_old_monthly_workbooks(
        monthly_dir, archive_root, retention_days, now_date):
    cutoff = now_date - timedelta(days=retention_days)
    archived = 0
    try:
        names = os.listdir(monthly_dir)
    except OSError:
        return archived
    for name in names:
        match = MONTHLY_FILE_PATTERN.fullmatch(name)
        if not match:
            continue
        try:
            month_start = datetime.strptime(match.group(1), '%Y-%m').date()
        except ValueError:
            continue
        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1, month=1
            )
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        month_end = next_month - timedelta(days=1)
        if month_end >= cutoff:
            continue
        try:
            archive_dir = os.path.join(archive_root, 'monthly')
            os.makedirs(archive_dir, exist_ok=True)
            source = os.path.join(monthly_dir, name)
            target = os.path.join(archive_dir, name)
            if os.path.exists(target):
                LOGGER.error(
                    'archive target already exists; keeping workbook %s', source
                )
                continue
            os.replace(source, target)
            archived += 1
        except OSError as exc:
            LOGGER.error('cannot archive monthly workbook %s: %s', name, exc)
    return archived
