#!/usr/bin/env python3
import sys
import argparse
from datetime import datetime, timedelta, timezone
from stats_core import (
    calculate_coverage, calculate_overall, calculate_stats, format_coverage,
    read_daily_data, read_sample_status,
)

CST = timezone(timedelta(hours=8))

def print_stats(stats, period_name, coverage=None):
    if not stats:
        print(f'\n{period_name}: No data available')
        print(format_coverage(coverage))
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
    print(format_coverage(coverage))

def query_daily(date):
    date_str = date.strftime('%Y-%m-%d')
    data = read_daily_data(date_str)
    stats = calculate_stats(data)
    coverage = calculate_coverage(read_sample_status(date_str))
    print_stats(stats, f'Daily: {date_str}', coverage)

def query_weekly(week_start):
    all_data = []
    all_status = []
    
    for i in range(7):
        date = week_start + timedelta(days=i)
        date_str = date.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
        all_status.extend(read_sample_status(date_str))
    
    stats = calculate_stats(all_data)
    print_stats(stats, f'Weekly: {week_start.strftime("%Y-%m-%d")} to {(week_start + timedelta(days=6)).strftime("%Y-%m-%d")}', calculate_coverage(all_status))

def query_monthly(year, month):
    first_day = datetime(year, month, 1).date()
    if month == 12:
        last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)
    
    all_data = []
    all_status = []
    current = first_day
    while current <= last_day:
        date_str = current.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
        all_status.extend(read_sample_status(date_str))
        current += timedelta(days=1)
    
    stats = calculate_stats(all_data)
    print_stats(stats, f'Monthly: {year}-{month:02d}', calculate_coverage(all_status))

def query_range(start_date, end_date):
    if start_date > end_date:
        raise ValueError('start date must not be after end date')
    all_data = []
    all_status = []
    current = start_date
    while current <= end_date:
        date_str = current.strftime('%Y-%m-%d')
        data = read_daily_data(date_str)
        if data:
            all_data.extend(data)
        all_status.extend(read_sample_status(date_str))
        current += timedelta(days=1)
    
    stats = calculate_stats(all_data)
    print_stats(stats, f'Range: {start_date} to {end_date}', calculate_coverage(all_status))

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
        except ValueError as e:
            print(f'Error: {e}. Use YYYY-MM-DD')
            sys.exit(1)

if __name__ == '__main__':
    main()
