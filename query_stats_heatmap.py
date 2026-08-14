#!/usr/bin/env python3
import sys
import argparse
from datetime import datetime, timedelta, timezone
from stats_core import calculate_overall, calculate_stats, parse_timestamp, read_daily_data

CST = timezone(timedelta(hours=8))

# ANSI 颜色代码
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
GRAY = '\033[90m'
RESET = '\033[0m'

def print_stats(stats, period_name):
    if not stats:
        print(f'\n{period_name}: No data available')
        return
    
    print(f'\n{period_name}')
    print('='*70)
    print(f'{"Card ID":<10}{"Samples":<12}{"Avg %":<12}{"Max %":<12}{"Min %":<12}')
    print('-'*70)
    
    for card_id in sorted(stats.keys()):
        stat = stats[card_id]
        print(f'{card_id:<10}{stat["count"]:<12}{stat["avg"]:<12.2f}{stat["max"]:<12.2f}{stat["min"]:<12.2f}')
    total_samples, overall_avg = calculate_overall(stats)
    print('-'*70)
    print(f'{"Overall":<10}{total_samples:<12}{overall_avg:<12.2f}')
    print('='*70)

def get_color(avg):
    """根据利用率返回对应的颜色"""
    if avg >= 80.0:
        return RED       # 高利用率：红色
    elif avg >= 40.0:
        return YELLOW    # 中利用率：黄色
    else:
        return GREEN     # 低利用率：绿色

def print_heatmap_matrix(start_date, end_date, all_data):
    """打印热力图二维矩阵"""
    if not all_data:
        return
        
    matrix = {}
    hourly_matrix = {}
    for row in all_data:
        try:
            ts = parse_timestamp(row['timestamp'])
            date_str = ts.strftime('%Y-%m-%d')
            hour = ts.hour
            key = (date_str, hour)
            if key not in matrix:
                matrix[key] = []
            matrix[key].append(row['utilization'])
            if hour not in hourly_matrix:
                hourly_matrix[hour] = []
            hourly_matrix[hour].append(row['utilization'])
        except Exception:
            continue
            
    print(f'\nHeatmap Matrix: {start_date} to {end_date}')
    print('='*95)
    header = "Date       | " + " ".join([f"{h:02d}" for h in range(24)])
    print(header)
    print('-'*95)
    
    current = start_date
    while current <= end_date:
        date_str = current.strftime('%Y-%m-%d')
        row_str = f"{date_str} | "
        for hour in range(24):
            samples = matrix.get((date_str, hour), [])
            if not samples:
                val_str = f"{GRAY}--{RESET}"
            else:
                avg = sum(samples) / len(samples)
                color = get_color(avg)
                val_str = f"{color}{int(avg):02d}{RESET}"
            row_str += val_str + " "
        print(row_str)
        current += timedelta(days=1)
    
    print('-'*95)
    summary_row = "Average    | "
    for hour in range(24):
        samples = hourly_matrix.get(hour, [])
        if not samples:
            val_str = f"{GRAY}--{RESET}"
        else:
            avg = sum(samples) / len(samples)
            color = get_color(avg)
            val_str = f"{color}{int(avg):02d}{RESET}"
        summary_row += val_str + " "
    print(summary_row)
    print('='*95)
    print(f"Legend: {GRAY}--{RESET} (No Data) | {GREEN}00-39{RESET} (Low) | {YELLOW}40-79{RESET} (Medium) | {RED}80-99{RESET} (High)")

def query_daily(date):
    date_str = date.strftime('%Y-%m-%d')
    data = read_daily_data(date_str)
    stats = calculate_stats(data)
    print_stats(stats, f'Daily: {date_str}')
    
    # 追加热力图
    print_heatmap_matrix(date, date, data)

def query_weekly(week_start):
    all_data = []
    week_end = week_start + timedelta(days=6)
    for i in range(7):
        date = week_start + timedelta(days=i)
        date_str = date.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
    stats = calculate_stats(all_data)
    print_stats(stats, f'Weekly: {week_start.strftime("%Y-%m-%d")} to {week_end.strftime("%Y-%m-%d")}')
    
    # 追加热力图
    print_heatmap_matrix(week_start, week_end, all_data)

def query_monthly(year, month):
    first_day = datetime(year, month, 1).date()
    if month == 12:
        last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)
    
    all_data = []
    current = first_day
    while current <= last_day:
        date_str = current.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
        current += timedelta(days=1)
    
    stats = calculate_stats(all_data)
    print_stats(stats, f'Monthly: {year}-{month:02d}')
    
    # 追加热力图
    print_heatmap_matrix(first_day, last_day, all_data)

def query_range(start_date, end_date):
    all_data = []
    current = start_date
    while current <= end_date:
        date_str = current.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
        current += timedelta(days=1)
    
    stats = calculate_stats(all_data)
    print_stats(stats, f'Range: {start_date} to {end_date}')
    
    # 追加热力图
    print_heatmap_matrix(start_date, end_date, all_data)

def main():
    parser = argparse.ArgumentParser(
        description='Query NPU utilization statistics',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s --today              Show today's statistics
  %(prog)s --yesterday          Show yesterday's statistics
  %(prog)s --week               Show this week's statistics
  %(prog)s --month              Show this month's statistics
  %(prog)s --date 2026-01-15    Show specific date statistics
  %(prog)s --monthly 2026-01    Show specific month statistics
  %(prog)s --range 2026-01-01 2026-01-31  Show date range statistics
        '''
    )
    
    parser.add_argument('--today', action='store_true', help='Show today\'s statistics')
    parser.add_argument('--yesterday', action='store_true', help='Show yesterday\'s statistics')
    parser.add_argument('--week', action='store_true', help='Show this week\'s statistics')
    parser.add_argument('--month', action='store_true', help='Show this month\'s statistics')
    parser.add_argument('--date', type=str, help='Show specific date (YYYY-MM-DD)')
    parser.add_argument('--monthly', type=str, help='Show specific month (YYYY-MM)')
    parser.add_argument('--range', nargs=2, metavar=('START', 'END'), help='Show date range (YYYY-MM-DD YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    
    if args.today:
        query_daily(datetime.now(CST).date())
    
    if args.yesterday:
        query_daily(datetime.now(CST).date() - timedelta(days=1))
    
    if args.date:
        try:
            date = datetime.strptime(args.date, '%Y-%m-%d').date()
            query_daily(date)
        except ValueError:
            print('Error: Invalid date format. Use YYYY-MM-DD')
            sys.exit(1)
    
    if args.week:
        today = datetime.now(CST).date()
        week_start = today - timedelta(days=today.weekday())
        query_weekly(week_start)
    
    if args.month:
        now = datetime.now(CST)
        query_monthly(now.year, now.month)
    
    if args.monthly:
        try:
            date = datetime.strptime(args.monthly, '%Y-%m')
            query_monthly(date.year, date.month)
        except ValueError:
            print('Error: Invalid month format. Use YYYY-MM')
            sys.exit(1)
    
    if args.range:
        try:
            start_date = datetime.strptime(args.range[0], '%Y-%m-%d').date()
            end_date = datetime.strptime(args.range[1], '%Y-%m-%d').date()
            query_range(start_date, end_date)
        except ValueError:
            print('Error: Invalid date format. Use YYYY-MM-DD')
            sys.exit(1)

if __name__ == '__main__':
    main()
