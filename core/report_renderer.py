"""
Report rendering — standalone HTML and Markdown reports.

Generates downloadable report files with Plotly charts (HTML)
or formatted tables (Markdown).
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime

from core.report_data import TestResult, ParetoPoint

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    logging.warning("Plotly not available. Install with: pip install plotly")

logger = logging.getLogger(__name__)


class ReportRenderer:
    """Render optimization reports as HTML or Markdown files."""

    def __init__(self, db_path: str = '/mnt/storage/serveit.db'):
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Plotly chart builders (for standalone HTML report)
    # ------------------------------------------------------------------

    def create_pareto_frontier_chart(self, pareto: List[ParetoPoint]) -> Optional['go.Figure']:
        if not PLOTLY_AVAILABLE or not pareto:
            return None

        arch_colors = {'aggregated': '#1f77b4', 'pd': '#ff7f0e', 'ep': '#2ca02c'}
        fig = go.Figure()

        for arch, color in arch_colors.items():
            arch_points = [p for p in pareto if p.config.architecture == arch]
            if not arch_points:
                continue
            fig.add_trace(go.Scatter(
                x=[p.cost for p in arch_points],
                y=[p.ttft for p in arch_points],
                mode='markers+lines',
                name=arch.upper(),
                marker=dict(size=15, color=color, symbol='diamond',
                            line=dict(width=2, color='white')),
                text=[f"{p.config.config_name}<br>"
                      f"TTFT: {p.ttft:.1f}ms<br>"
                      f"Throughput: {p.throughput:.2f} req/s<br>"
                      f"Efficiency: {p.efficiency:.3f} req/s/GPU"
                      for p in arch_points],
                hovertemplate='<b>%{text}</b><extra></extra>',
                line=dict(width=2, dash='dot')
            ))

        fig.update_layout(
            title='Pareto Frontier: Optimal Latency-Cost Trade-offs',
            xaxis_title='Total GPUs', yaxis_title='TTFT P90 (ms)',
            hovermode='closest', template='plotly_white', height=500,
            showlegend=True,
            legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99)
        )
        return fig

    def create_throughput_vs_latency_chart(self, results: List[TestResult]) -> Optional['go.Figure']:
        if not PLOTLY_AVAILABLE:
            return None
        successful = [r for r in results if r.is_successful]
        if not successful:
            return None

        arch_colors = {'aggregated': '#1f77b4', 'pd': '#ff7f0e', 'ep': '#2ca02c'}
        fig = go.Figure()

        for arch, color in arch_colors.items():
            arch_results = [r for r in successful if r.architecture == arch]
            if not arch_results:
                continue
            fig.add_trace(go.Scatter(
                x=[r.ttft_p90 for r in arch_results],
                y=[r.throughput_p90 for r in arch_results],
                mode='markers', name=arch.upper(),
                marker=dict(size=[r.total_gpus * 2 for r in arch_results],
                            color=color, opacity=0.7,
                            line=dict(width=1, color='white')),
                text=[f"{r.config_name}<br>"
                      f"TTFT: {r.ttft_p90:.1f}ms<br>"
                      f"Throughput: {r.throughput_p90:.2f} req/s<br>"
                      f"GPUs: {r.total_gpus}"
                      for r in arch_results],
                hovertemplate='<b>%{text}</b><extra></extra>'
            ))

        fig.update_layout(
            title='Throughput vs Latency (bubble size = GPU count)',
            xaxis_title='TTFT P90 (ms)', yaxis_title='Throughput P90 (req/s)',
            hovermode='closest', template='plotly_white', height=500, showlegend=True
        )
        return fig

    def create_efficiency_chart(self, results: List[TestResult]) -> Optional['go.Figure']:
        if not PLOTLY_AVAILABLE:
            return None
        successful = [r for r in results if r.is_successful]
        if not successful:
            return None

        configs_with_efficiency = sorted(
            [(r.config_name, r.throughput_p90 / r.total_gpus, r.architecture) for r in successful],
            key=lambda x: x[1], reverse=True
        )[:15]

        colors = ['#1f77b4' if arch == 'aggregated' else '#ff7f0e' if arch == 'pd' else '#2ca02c'
                  for _, _, arch in configs_with_efficiency]

        fig = go.Figure(data=[go.Bar(
            x=[name for name, _, _ in configs_with_efficiency],
            y=[eff for _, eff, _ in configs_with_efficiency],
            marker_color=colors,
            text=[f'{eff:.3f}' for _, eff, _ in configs_with_efficiency],
            textposition='outside',
            hovertemplate='<b>%{x}</b><br>Efficiency: %{y:.3f} req/s/GPU<extra></extra>'
        )])

        fig.update_layout(
            title='Top 15 Most Efficient Configurations (Throughput per GPU)',
            xaxis_title='Configuration', yaxis_title='Efficiency (req/s/GPU)',
            template='plotly_white', height=500, xaxis=dict(tickangle=-45)
        )
        return fig

    def create_architecture_comparison_chart(self, stats: Dict[str, Any]) -> Optional['go.Figure']:
        if not PLOTLY_AVAILABLE or 'by_architecture' not in stats:
            return None

        by_arch = stats['by_architecture']
        if not by_arch:
            return None

        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Average TTFT P90', 'Average Throughput P90',
                            'Average GPUs', 'Best Configurations'),
            specs=[[{'type': 'bar'}, {'type': 'bar'}],
                   [{'type': 'bar'}, {'type': 'bar'}]]
        )

        architectures = list(by_arch.keys())
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        fig.add_trace(go.Bar(x=architectures, y=[by_arch[a]['avg_ttft_p90'] for a in architectures],
                             name='Avg TTFT', marker_color=colors[:len(architectures)], showlegend=False), row=1, col=1)
        fig.add_trace(go.Bar(x=architectures, y=[by_arch[a]['avg_throughput_p90'] for a in architectures],
                             name='Avg Throughput', marker_color=colors[:len(architectures)], showlegend=False), row=1, col=2)
        fig.add_trace(go.Bar(x=architectures, y=[by_arch[a]['avg_gpus'] for a in architectures],
                             name='Avg GPUs', marker_color=colors[:len(architectures)], showlegend=False), row=2, col=1)
        fig.add_trace(go.Bar(x=architectures, y=[by_arch[a]['best_ttft'] for a in architectures],
                             name='Best TTFT', marker_color=colors[:len(architectures)], showlegend=False), row=2, col=2)

        fig.update_yaxes(title_text="ms", row=1, col=1)
        fig.update_yaxes(title_text="req/s", row=1, col=2)
        fig.update_yaxes(title_text="count", row=2, col=1)
        fig.update_yaxes(title_text="ms", row=2, col=2)

        fig.update_layout(title_text='Architecture Comparison', height=700,
                          template='plotly_white', showlegend=False)
        return fig

    # ------------------------------------------------------------------
    # Full standalone HTML report
    # ------------------------------------------------------------------

    def generate_html_report(self, results, pareto, stats, output_path: str) -> str:
        if not PLOTLY_AVAILABLE:
            logger.error("Plotly not available. Cannot generate HTML report.")
            return None

        pareto_chart = self.create_pareto_frontier_chart(pareto)
        scatter_chart = self.create_throughput_vs_latency_chart(results)
        efficiency_chart = self.create_efficiency_chart(results)
        arch_chart = self.create_architecture_comparison_chart(stats)

        html_parts = []
        html_parts.append(f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>ServeIt Studio Optimization Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; max-width: 1400px; margin: 0 auto; padding: 20px; background-color: #f5f5f5; }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #34495e; margin-top: 40px; border-bottom: 2px solid #95a5a6; padding-bottom: 8px; }}
        .header {{ background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stat-card h3 {{ margin-top: 0; color: #3498db; }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #2c3e50; }}
        .stat-label {{ color: #7f8c8d; font-size: 0.9em; }}
        .chart-container {{ background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        table {{ width: 100%; border-collapse: collapse; background: white; margin: 20px 0; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        th {{ background-color: #3498db; color: white; padding: 12px; text-align: left; }}
        td {{ padding: 10px; border-bottom: 1px solid #ecf0f1; }}
        tr:hover {{ background-color: #f8f9fa; }}
        .success {{ color: #27ae60; }}
        .failed {{ color: #e74c3c; }}
        code {{ background: #ecf0f1; padding: 2px 6px; border-radius: 3px; font-family: 'Courier New', monospace; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>ServeIt Studio Optimization Report</h1>
        <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>Database:</strong> <code>{self.db_path}</code></p>
    </div>
    <div class="stats">
        <div class="stat-card"><h3>Total Tests</h3><div class="stat-value">{stats['total_tests']}</div></div>
        <div class="stat-card"><h3>Successful Tests</h3><div class="stat-value success">{stats['successful_tests']}</div></div>
        <div class="stat-card"><h3>Failed Tests</h3><div class="stat-value failed">{stats['failed_tests']}</div></div>
        <div class="stat-card"><h3>Broken Tests</h3><div class="stat-value" style="color: #f39c12;">{stats.get('broken_tests', 0)}</div><div class="stat-label">Completed with zero metrics</div></div>
''')

        if 'best_configs' in stats:
            best = stats['best_configs']['most_efficient']
            html_parts.append(f'''
        <div class="stat-card"><h3>Best Efficiency</h3><div class="stat-value">{best['efficiency']:.3f}</div><div class="stat-label">req/s/GPU</div><div class="stat-label"><code>{best['name']}</code></div></div>
''')

        html_parts.append('    </div>')

        # Charts
        for chart, include_js in [(pareto_chart, True), (arch_chart, False),
                                   (scatter_chart, False), (efficiency_chart, False)]:
            if chart:
                html_parts.append('<div class="chart-container">')
                html_parts.append(chart.to_html(full_html=False,
                                                include_plotlyjs='cdn' if include_js else False))
                html_parts.append('</div>')

        # Pareto frontier table
        if pareto:
            html_parts.append('''
    <h2>Pareto Frontier</h2>
    <p>Configurations representing optimal trade-offs between latency, throughput, and cost.</p>
    <table>
        <tr><th>Configuration</th><th>TTFT P90 (ms)</th><th>Throughput P90 (req/s)</th><th>GPUs</th><th>Efficiency (req/s/GPU)</th><th>Architecture</th></tr>
''')
            for point in pareto:
                html_parts.append(f'''        <tr><td><code>{point.config.config_name}</code></td><td>{point.ttft:.2f}</td><td>{point.throughput:.2f}</td><td>{point.cost}</td><td>{point.efficiency:.3f}</td><td>{point.config.architecture.upper()}</td></tr>
''')
            html_parts.append('    </table>')

        # All successful results table
        successful = [r for r in results if r.is_successful]
        if successful:
            html_parts.append('''
    <h2>All Successful Configurations</h2>
    <table>
        <tr><th>Configuration</th><th>TTFT P90 (ms)</th><th>ITL P90 (ms)</th><th>ITL P95 (ms)</th><th>ITL P99 (ms)</th><th>Throughput P90 (req/s)</th><th>GPUs</th><th>GPU Util (%)</th><th>KV Cache (%)</th><th>Efficiency</th><th>Architecture</th></tr>
''')
            for result in sorted(successful, key=lambda r: r.ttft_p90):
                eff = result.throughput_p90 / result.total_gpus
                itl90 = f"{result.itl_p90:.2f}" if result.itl_p90 else "N/A"
                itl95 = f"{result.itl_p95:.2f}" if result.itl_p95 else "N/A"
                itl99 = f"{result.itl_p99:.2f}" if result.itl_p99 else "N/A"
                gpu_str = f"{result.gpu_utilization:.1f}" if result.gpu_utilization else "N/A"
                kv_str = f"{result.kv_cache_usage:.4f}" if result.kv_cache_usage else "N/A"
                html_parts.append(f'''        <tr><td><code>{result.config_name}</code></td><td>{result.ttft_p90:.2f}</td><td>{itl90}</td><td>{itl95}</td><td>{itl99}</td><td>{result.throughput_p90:.2f}</td><td>{result.total_gpus}</td><td>{gpu_str}</td><td>{kv_str}</td><td>{eff:.3f}</td><td>{result.architecture.upper()}</td></tr>
''')
            html_parts.append('    </table>')

        # Failed & broken tests
        failed = [r for r in results if r.status == 'failed']
        broken = [r for r in results if r.status == 'completed' and not r.is_successful]
        failed_and_broken = failed + broken

        if failed_and_broken:
            html_parts.append('''
    <h2>Failed &amp; Broken Tests</h2>
    <table>
        <tr><th>Configuration</th><th>Reason</th><th>TTFT P90</th><th>Throughput P90</th><th>Prefill Pods</th><th>Decode Pods</th><th>TP</th><th>Architecture</th></tr>
''')
            for result in failed_and_broken:
                reason = 'Deploy/benchmark failed' if result.status == 'failed' else 'Zero metrics (gateway error)'
                color = '#e74c3c' if result.status == 'failed' else '#f39c12'
                ttft_str = f"{result.ttft_p90:.2f}" if result.ttft_p90 is not None else "N/A"
                tput_str = f"{result.throughput_p90:.2f}" if result.throughput_p90 is not None else "N/A"
                html_parts.append(f'''        <tr><td><code>{result.config_name}</code></td><td style="color: {color}">{reason}</td><td>{ttft_str}</td><td>{tput_str}</td><td>{result.prefill_pods}</td><td>{result.decode_pods}</td><td>{result.tensor_parallelism}</td><td>{result.architecture.upper()}</td></tr>
''')
            html_parts.append('    </table>')

        html_parts.append('</body>\n</html>')

        html = '\n'.join(html_parts)
        Path(output_path).write_text(html)
        logger.info(f"HTML report generated: {output_path}")
        return html

    # ------------------------------------------------------------------
    # Markdown report
    # ------------------------------------------------------------------

    def generate_markdown_report(self, results, pareto, stats, output_path: str) -> str:
        lines = []
        lines.append("# ServeIt Studio Optimization Report")
        lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"\nDatabase: `{self.db_path}`")
        lines.append("\n---\n")

        # Executive Summary
        lines.append("## Executive Summary\n")
        lines.append(f"- **Total Tests**: {stats['total_tests']}")
        lines.append(f"- **Successful Tests**: {stats['successful_tests']}")
        lines.append(f"- **Failed Tests**: {stats['failed_tests']}")
        lines.append(f"- **Broken Tests** (completed with zero metrics): {stats.get('broken_tests', 0)}")
        lines.append("")

        if 'best_configs' in stats:
            lines.append("### Best Configurations\n")
            best = stats['best_configs']

            lines.append("#### Lowest Latency")
            lines.append(f"- **Configuration**: `{best['lowest_latency']['name']}`")
            lines.append(f"- **TTFT P90**: {best['lowest_latency']['ttft_p90']:.2f} ms")
            lines.append(f"- **Throughput P90**: {best['lowest_latency']['throughput_p90']:.2f} req/s")
            lines.append(f"- **GPUs**: {best['lowest_latency']['gpus']}")
            lines.append("")

            lines.append("#### Highest Throughput")
            lines.append(f"- **Configuration**: `{best['highest_throughput']['name']}`")
            lines.append(f"- **TTFT P90**: {best['highest_throughput']['ttft_p90']:.2f} ms")
            lines.append(f"- **Throughput P90**: {best['highest_throughput']['throughput_p90']:.2f} req/s")
            lines.append(f"- **GPUs**: {best['highest_throughput']['gpus']}")
            lines.append("")

            lines.append("#### Most Efficient (Throughput/GPU)")
            lines.append(f"- **Configuration**: `{best['most_efficient']['name']}`")
            lines.append(f"- **TTFT P90**: {best['most_efficient']['ttft_p90']:.2f} ms")
            lines.append(f"- **Throughput P90**: {best['most_efficient']['throughput_p90']:.2f} req/s")
            lines.append(f"- **GPUs**: {best['most_efficient']['gpus']}")
            lines.append(f"- **Efficiency**: {best['most_efficient']['efficiency']:.3f} req/s/GPU")
            lines.append("")

        # Architecture Comparison
        if 'by_architecture' in stats and stats['by_architecture']:
            lines.append("## Architecture Comparison\n")
            lines.append("| Architecture | Tests | Avg TTFT P90 (ms) | Avg Throughput P90 (req/s) | Avg GPUs | Best TTFT (ms) | Best Throughput (req/s) |")
            lines.append("|--------------|-------|-------------------|---------------------------|----------|----------------|------------------------|")

            for arch, data in stats['by_architecture'].items():
                lines.append(f"| {arch.upper()} | {data['count']} | "
                           f"{data['avg_ttft_p90']:.2f} | "
                           f"{data['avg_throughput_p90']:.2f} | "
                           f"{data['avg_gpus']:.1f} | "
                           f"{data['best_ttft']:.2f} | "
                           f"{data['best_throughput']:.2f} |")
            lines.append("")

        # Pareto Frontier
        lines.append("## Pareto Frontier\n")
        lines.append("Configurations on the Pareto frontier represent optimal trade-offs between ")
        lines.append("latency, throughput, and resource cost.\n")

        if pareto:
            lines.append("| Configuration | TTFT P90 (ms) | Throughput P90 (req/s) | GPUs | Efficiency (req/s/GPU) | Architecture |")
            lines.append("|---------------|---------------|------------------------|------|------------------------|--------------|")

            for point in pareto:
                lines.append(f"| `{point.config.config_name}` | "
                           f"{point.ttft:.2f} | "
                           f"{point.throughput:.2f} | "
                           f"{point.cost} | "
                           f"{point.efficiency:.3f} | "
                           f"{point.config.architecture.upper()} |")
            lines.append("")

            lines.append("### Recommendations\n")
            if len(pareto) > 0:
                lowest_latency = min(pareto, key=lambda p: p.ttft)
                lines.append(f"**For latency-sensitive workloads**: Use `{lowest_latency.config.config_name}` ")
                lines.append(f"({lowest_latency.ttft:.2f}ms TTFT, {lowest_latency.cost} GPUs)\n")

                highest_throughput = max(pareto, key=lambda p: p.throughput)
                lines.append(f"**For maximum throughput**: Use `{highest_throughput.config.config_name}` ")
                lines.append(f"({highest_throughput.throughput:.2f} req/s, {highest_throughput.cost} GPUs)\n")

                most_efficient = max(pareto, key=lambda p: p.efficiency)
                lines.append(f"**For cost efficiency**: Use `{most_efficient.config.config_name}` ")
                lines.append(f"({most_efficient.efficiency:.3f} req/s/GPU, {most_efficient.cost} GPUs)\n")
        else:
            lines.append("*No configurations on Pareto frontier (insufficient successful tests)*\n")

        # Detailed Results
        successful = [r for r in results if r.is_successful]
        if successful:
            lines.append("## Detailed Results\n")
            lines.append("| Configuration | TTFT P90 (ms) | ITL P90 (ms) | ITL P95 (ms) | ITL P99 (ms) | Throughput P90 (req/s) | GPUs | GPU Util (%) | KV Cache (%) | TP | Architecture |")
            lines.append("|---------------|---------------|--------------|--------------|--------------|------------------------|------|--------------|--------------|----|--------------|")

            for result in sorted(successful, key=lambda r: r.ttft_p90):
                itl90 = f"{result.itl_p90:.2f}" if result.itl_p90 else "N/A"
                itl95 = f"{result.itl_p95:.2f}" if result.itl_p95 else "N/A"
                itl99 = f"{result.itl_p99:.2f}" if result.itl_p99 else "N/A"
                gpu_str = f"{result.gpu_utilization:.1f}" if result.gpu_utilization else "N/A"
                kv_str = f"{result.kv_cache_usage:.4f}" if result.kv_cache_usage else "N/A"
                lines.append(f"| `{result.config_name}` | "
                           f"{result.ttft_p90:.2f} | "
                           f"{itl90} | "
                           f"{itl95} | "
                           f"{itl99} | "
                           f"{result.throughput_p90:.2f} | "
                           f"{result.total_gpus} | "
                           f"{gpu_str} | "
                           f"{kv_str} | "
                           f"{result.tensor_parallelism} | "
                           f"{result.architecture.upper()} |")
            lines.append("")

        # Failed and broken tests
        failed = [r for r in results if r.status == 'failed']
        broken = [r for r in results if r.status == 'completed' and not r.is_successful]
        failed_and_broken = failed + broken

        if failed_and_broken:
            lines.append("## Failed & Broken Tests\n")
            lines.append("| Configuration | Reason | TTFT P90 | Throughput P90 | Prefill Pods | Decode Pods | TP | Architecture |")
            lines.append("|---------------|--------|----------|----------------|--------------|-------------|----|--------------|")

            for result in failed_and_broken:
                reason = "Deploy/benchmark failed" if result.status == 'failed' else "Zero metrics (gateway error)"
                ttft_str = f"{result.ttft_p90:.2f}" if result.ttft_p90 is not None else "N/A"
                tput_str = f"{result.throughput_p90:.2f}" if result.throughput_p90 is not None else "N/A"
                lines.append(f"| `{result.config_name}` | "
                           f"{reason} | "
                           f"{ttft_str} | "
                           f"{tput_str} | "
                           f"{result.prefill_pods} | "
                           f"{result.decode_pods} | "
                           f"{result.tensor_parallelism} | "
                           f"{result.architecture.upper()} |")
            lines.append("")

        report = '\n'.join(lines)
        Path(output_path).write_text(report)
        logger.info(f"Report generated: {output_path}")
        return report
