"""
ServeIt Studio Optimization Report Generator — backward-compatibility facade.

This module re-exports everything from the split modules so that
existing callers (e.g. `from core.report_generator import ReportGenerator`)
continue to work unchanged.

Internal structure:
    report_data.py      — TestResult, ParetoPoint, ReportDataLoader
    report_analysis.py  — ReportAnalyzer (Pareto, stats, recommendations, charts)
    report_renderer.py  — ReportRenderer (standalone HTML / Markdown reports)
"""

from typing import Optional

from core.report_data import ReportDataLoader
from core.report_analysis import ReportAnalyzer
from core.report_renderer import ReportRenderer


class ReportGenerator:
    """Facade that combines data loading, analysis, and rendering."""

    def __init__(self, db_path: str = '/mnt/storage/serveit.db'):
        self.db_path = db_path
        self._loader = ReportDataLoader(db_path)
        self._analyzer = ReportAnalyzer()
        self._renderer = ReportRenderer(db_path)

    def __enter__(self):
        self._loader.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._loader.__exit__(exc_type, exc_val, exc_tb)

    # Expose the DB connection for callers that need it
    @property
    def conn(self):
        return self._loader.conn

    # --- Data loading (delegate to ReportDataLoader) ---
    def get_all_test_results(self, run_id=None):
        return self._loader.get_all_test_results(run_id)

    # --- Analysis (delegate to ReportAnalyzer) ---
    def calculate_pareto_frontier(self, results, metric='ttft_p99', throughput_metric='throughput_p90'):
        return self._analyzer.calculate_pareto_frontier(results, metric, throughput_metric)

    def get_summary_statistics(self, results):
        return self._analyzer.get_summary_statistics(results)

    # --- Rendering (delegate to ReportRenderer) ---
    def generate_html_report(self, output_path, run_id=None):
        results = self.get_all_test_results(run_id)
        pareto = self.calculate_pareto_frontier(results)
        stats = self.get_summary_statistics(results)
        return self._renderer.generate_html_report(results, pareto, stats, output_path)

    def generate_markdown_report(self, output_path, run_id=None):
        results = self.get_all_test_results(run_id)
        pareto = self.calculate_pareto_frontier(results)
        stats = self.get_summary_statistics(results)
        return self._renderer.generate_markdown_report(results, pareto, stats, output_path)


# Convenience functions
def generate_html_report(db_path: str = '/mnt/storage/serveit.db',
                         output_path: str = '/mnt/storage/optimization_report.html',
                         run_id: Optional[int] = None) -> str:
    with ReportGenerator(db_path) as gen:
        return gen.generate_html_report(output_path, run_id)


def generate_markdown_report(db_path: str = '/mnt/storage/serveit.db',
                              output_path: str = '/mnt/storage/optimization_report.md',
                              run_id: Optional[int] = None) -> str:
    with ReportGenerator(db_path) as gen:
        return gen.generate_markdown_report(output_path, run_id)


generate_report = generate_markdown_report


if __name__ == '__main__':
    print("Generating HTML report...")
    html = generate_html_report()
    print(f"HTML report generated: {len(html)} bytes")

    print("\nGenerating Markdown report...")
    md = generate_markdown_report()
    print(f"Markdown report generated: {len(md)} bytes")
