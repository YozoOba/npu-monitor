from datetime import datetime, timedelta, timezone
import os
import tempfile
import zipfile
from xml.sax.saxutils import escape, quoteattr

from cluster_common.protocol import ProtocolError, parse_timestamp
from cluster_common.timezones import CHINA_STANDARD_TIME


HEAT_COLORS = (
    'FFEAF4FB', 'FFD6EAF8', 'FFB9DDF2', 'FF91C9E8', 'FF62B0D9',
    'FF3696C5', 'FF187EAC', 'FF0C668F', 'FF075273', 'FF033E57',
)
STYLE_TITLE = 1
STYLE_HEADER = 2
STYLE_INTEGER = 3
STYLE_NUMBER = 4
STYLE_META_LABEL = 5
STYLE_HEAT_BASE = 6


def _parse_local_boundary(value, is_end=False):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('custom report requires start and end')
    value = value.strip()
    if len(value) == 10:
        parsed = datetime.strptime(value, '%Y-%m-%d').replace(
            tzinfo=CHINA_STANDARD_TIME
        )
        return parsed + timedelta(days=1) if is_end else parsed
    try:
        return parse_timestamp(value).astimezone(CHINA_STANDARD_TIME)
    except ProtocolError:
        pass
    for pattern in (
            '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
            '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, pattern).replace(
                tzinfo=CHINA_STANDARD_TIME
            )
        except ValueError:
            continue
    raise ValueError(
        'report time must be YYYY-MM-DD or ISO 8601; timezone-less values use UTC+08:00'
    )


def resolve_report_range(
        period, start=None, end=None, now=None, max_days=180):
    period = (period or 'month').strip().lower()
    if period not in ('month', 'day', 'custom'):
        raise ValueError('period must be month, day or custom')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(CHINA_STANDARD_TIME)
    if period == 'month':
        local_start = local_now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        local_end = local_now
    elif period == 'day':
        local_start = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        local_end = local_now
    else:
        local_start = _parse_local_boundary(start)
        local_end = _parse_local_boundary(end, is_end=True)
    if local_end <= local_start:
        raise ValueError('report end must be later than start')
    if (local_end - local_start).total_seconds() > max_days * 86400:
        raise ValueError('report range cannot exceed {} days'.format(max_days))
    return {
        'period': period,
        'local_start': local_start,
        'local_end': local_end,
        'utc_start': local_start.astimezone(timezone.utc).isoformat(
            timespec='seconds'
        ),
        'utc_end': local_end.astimezone(timezone.utc).isoformat(
            timespec='seconds'
        ),
    }


def report_dates(report_range):
    first = report_range['local_start'].date()
    last = (report_range['local_end'] - timedelta(microseconds=1)).date()
    values = []
    current = first
    while current <= last:
        values.append(current)
        current += timedelta(days=1)
    return values


def report_filename(report_range):
    start = report_range['local_start'].strftime('%Y%m%d-%H%M%S')
    end = report_range['local_end'].strftime('%Y%m%d-%H%M%S')
    return 'npu-utilization-{}-{}_to_{}.xlsx'.format(
        report_range['period'], start, end
    )


def _xml_text(value):
    return escape(str(value), {'"': '&quot;'})


def _column_name(index):
    value = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _cell(reference, value, style=0):
    if value is None or value == '':
        return ''
    style_text = '' if style == 0 else ' s="{}"'.format(style)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="{}"{}><v>{}</v></c>'.format(
            reference, style_text, value
        )
    return (
        '<c r="{}"{} t="inlineStr"><is><t>{}</t></is></c>'.format(
            reference, style_text, _xml_text(value)
        )
    )


def _heat_style(value):
    if value is None:
        return STYLE_NUMBER
    level = min(9, max(0, int(float(value) // 10)))
    return STYLE_HEAT_BASE + level


def _write_row(write, row_number, values, styles=None, height=None):
    height_text = (
        ' ht="{}" customHeight="1"'.format(height) if height else ''
    )
    write('<row r="{}"{}>'.format(row_number, height_text))
    styles = styles or ()
    for index, value in enumerate(values, start=1):
        style = styles[index - 1] if index <= len(styles) else 0
        write(_cell('{}{}'.format(_column_name(index), row_number), value, style))
    write('</row>')


def _period_title(period):
    return {
        'month': 'NPU 月度利用率汇总与热力图',
        'day': 'NPU 当日利用率汇总与热力图',
        'custom': 'NPU 自定义区间利用率汇总与热力图',
    }[period]


def _metadata(report_range, generated_at):
    return (
        ('查询类型', report_range['period']),
        ('查询范围', '{} 至 {}（结束时间不包含）'.format(
            report_range['local_start'].isoformat(timespec='seconds'),
            report_range['local_end'].isoformat(timespec='seconds'),
        )),
        ('时区', 'UTC+08:00'),
        ('生成时间', generated_at.isoformat(timespec='seconds')),
        ('计算口径', '单节点全部逐卡采样点的算术平均值'),
    )


def _write_summary_sheet(
        archive, sheet_index, report_range, aggregate, dates, generated_at):
    path = 'xl/worksheets/sheet{}.xml'.format(sheet_index)
    with archive.open(path, 'w') as output:
        def write(value):
            output.write(value.encode('utf-8'))

        last_column = _column_name(6 + len(dates))
        write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
        write('<sheetViews><sheetView workbookViewId="0">')
        write('<pane xSplit="3" ySplit="7" topLeftCell="D8" activePane="bottomRight" state="frozen"/>')
        write('</sheetView></sheetViews><cols>')
        widths = [18, 24, 24, 18, 16, 14] + [12] * len(dates)
        for index, width in enumerate(widths, start=1):
            write('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(
                index, width
            ))
        write('</cols><sheetData>')
        _write_row(
            write, 1, [_period_title(report_range['period'])],
            [STYLE_TITLE], height=26,
        )
        for row_number, (label, value) in enumerate(
                _metadata(report_range, generated_at), start=2):
            _write_row(
                write, row_number, [label, value],
                [STYLE_META_LABEL, 0],
            )
        headings = [
            '集群 ID', '节点 ID', '节点名称', '区间平均利用率(%)',
            '逐卡数据点数', '有数据天数',
        ] + [value.strftime('%m-%d') for value in dates]
        _write_row(
            write, 7, headings, [STYLE_HEADER] * len(headings), height=24
        )
        row_number = 7
        for node in aggregate.get('nodes', []):
            row_number += 1
            day_values = [
                (node.get('days', {}).get(value.isoformat()) or {}).get(
                    'utilization_avg'
                ) for value in dates
            ]
            values = [
                node['cluster_id'], node['node_id'], node['node_name'],
                node['utilization_avg'], node['card_samples'],
                sum(value is not None for value in day_values),
            ] + day_values
            styles = [0, 0, 0, _heat_style(node['utilization_avg']),
                      STYLE_INTEGER, STYLE_INTEGER]
            styles += [_heat_style(value) for value in day_values]
            _write_row(write, row_number, values, styles)
        if row_number == 7:
            row_number = 8
            _write_row(write, row_number, ['当前查询范围没有数据'])
        write('</sheetData>')
        write('<autoFilter ref="A7:{}{}"/>'.format(last_column, row_number))
        write('<mergeCells count="1"><mergeCell ref="A1:{}1"/></mergeCells>'.format(
            last_column
        ))
        write('</worksheet>')


def _write_daily_sheet(
        archive, sheet_index, day, report_range, aggregate, generated_at):
    path = 'xl/worksheets/sheet{}.xml'.format(sheet_index)
    with archive.open(path, 'w') as output:
        def write(value):
            output.write(value.encode('utf-8'))

        last_column = _column_name(5 + 24)
        write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
        write('<sheetViews><sheetView workbookViewId="0">')
        write('<pane xSplit="3" ySplit="7" topLeftCell="D8" activePane="bottomRight" state="frozen"/>')
        write('</sheetView></sheetViews><cols>')
        widths = [18, 24, 24, 18, 16] + [10] * 24
        for index, width in enumerate(widths, start=1):
            write('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(
                index, width
            ))
        write('</cols><sheetData>')
        _write_row(
            write, 1, ['{} 每小时利用率热力图'.format(day.isoformat())],
            [STYLE_TITLE], height=26,
        )
        metadata = (
            ('日期', day.isoformat()),
            ('时区', 'UTC+08:00'),
            ('查询范围', '{} 至 {}'.format(
                report_range['local_start'].isoformat(timespec='seconds'),
                report_range['local_end'].isoformat(timespec='seconds'),
            )),
            ('生成时间', generated_at.isoformat(timespec='seconds')),
            ('计算口径', '每小时内单节点全部逐卡采样点的算术平均值'),
        )
        for row_number, (label, value) in enumerate(metadata, start=2):
            _write_row(
                write, row_number, [label, value],
                [STYLE_META_LABEL, 0],
            )
        headings = [
            '集群 ID', '节点 ID', '节点名称', '当日平均利用率(%)',
            '逐卡数据点数',
        ] + ['{:02d}:00'.format(hour) for hour in range(24)]
        _write_row(
            write, 7, headings, [STYLE_HEADER] * len(headings), height=24
        )
        row_number = 7
        day_key = day.isoformat()
        for node in aggregate.get('nodes', []):
            day_value = node.get('days', {}).get(day_key) or {
                'utilization_avg': None, 'card_samples': 0, 'hours': {},
            }
            row_number += 1
            hour_values = [
                (day_value.get('hours', {}).get('{:02d}'.format(hour)) or {}).get(
                    'utilization_avg'
                ) for hour in range(24)
            ]
            values = [
                node['cluster_id'], node['node_id'], node['node_name'],
                day_value['utilization_avg'], day_value['card_samples'],
            ] + hour_values
            styles = [0, 0, 0, _heat_style(day_value['utilization_avg']),
                      STYLE_INTEGER]
            styles += [_heat_style(value) for value in hour_values]
            _write_row(write, row_number, values, styles)
        if row_number == 7:
            row_number = 8
            _write_row(write, row_number, ['当天没有数据'])
        write('</sheetData>')
        write('<autoFilter ref="A7:{}{}"/>'.format(last_column, row_number))
        write('<mergeCells count="1"><mergeCell ref="A1:{}1"/></mergeCells>'.format(
            last_column
        ))
        write('</worksheet>')


def _content_types(sheet_count):
    sheets = ''.join(
        '<Override PartName="/xl/worksheets/sheet{}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(
            index
        ) for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '{}'
        '</Types>'
    ).format(sheets)


def _workbook_xml(sheet_names):
    sheets = ''.join(
        '<sheet name={} sheetId="{}" r:id="rId{}"/>'.format(
            quoteattr(name), index, index
        ) for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView/></bookViews><sheets>{}</sheets></workbook>'
    ).format(sheets)


def _workbook_relationships(sheet_count):
    relationships = ''.join(
        '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{}.xml"/>'.format(
            index, index
        ) for index in range(1, sheet_count + 1)
    )
    relationships += (
        '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'.format(
            sheet_count + 1
        )
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '{}</Relationships>'
    ).format(relationships)


def _styles_xml():
    fills = (
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0F172A"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill>'
    )
    fills += ''.join(
        '<fill><patternFill patternType="solid"><fgColor rgb="{}"/><bgColor indexed="64"/></patternFill></fill>'.format(
            color
        ) for color in HEAT_COLORS
    )
    cell_xfs = (
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>'
    )
    cell_xfs += ''.join(
        '<xf numFmtId="164" fontId="{}" fillId="{}" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1" applyFont="1"/>'.format(
            2 if level >= 6 else 0, 5 + level
        ) for level in range(len(HEAT_COLORS))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>'
        '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="{}">{}</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="{}">{}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    ).format(5 + len(HEAT_COLORS), fills, 6 + len(HEAT_COLORS), cell_xfs)


ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)


def build_utilization_report(output_path, report_range, aggregate, now=None):
    generated_at = (now or datetime.now(timezone.utc)).astimezone(
        CHINA_STANDARD_TIME
    )
    dates = report_dates(report_range)
    sheet_names = ['汇总'] + [value.isoformat() for value in dates]
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.utilization-report-', suffix='.xlsx.tmp', dir=output_dir
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
                temporary_path, 'w', zipfile.ZIP_DEFLATED,
                allowZip64=True) as archive:
            archive.writestr('[Content_Types].xml', _content_types(len(sheet_names)))
            archive.writestr('_rels/.rels', ROOT_RELS)
            archive.writestr('xl/workbook.xml', _workbook_xml(sheet_names))
            archive.writestr(
                'xl/_rels/workbook.xml.rels',
                _workbook_relationships(len(sheet_names)),
            )
            archive.writestr('xl/styles.xml', _styles_xml())
            _write_summary_sheet(
                archive, 1, report_range, aggregate, dates, generated_at
            )
            for index, day in enumerate(dates, start=2):
                _write_daily_sheet(
                    archive, index, day, report_range, aggregate, generated_at
                )
        with open(temporary_path, 'rb+') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
    return {
        'path': output_path,
        'nodes': len(aggregate.get('nodes', [])),
        'days': len(dates),
        'card_samples': sum(
            node.get('card_samples', 0) for node in aggregate.get('nodes', [])
        ),
    }
