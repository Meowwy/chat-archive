<script>
	import {
		CategoryScale,
		Chart,
		Filler,
		Legend,
		LineController,
		LineElement,
		LinearScale,
		PointElement,
		Tooltip
	} from 'chart.js';
	import { formatCount, formatMonthShort } from '$lib/format.js';

	// Only the pieces a line chart needs, so the bundle carries nothing else.
	Chart.register(
		CategoryScale,
		Filler,
		Legend,
		LineController,
		LineElement,
		LinearScale,
		PointElement,
		Tooltip
	);

	/**
	 * Messages per month, as one line per series over a shared timeline.
	 *
	 * `labels` is every month in the range with its gaps filled, so a silent
	 * month is a zero rather than a straight line drawn across it, and every
	 * line carries one value per label. Whatever is on show owns the y-axis, so
	 * the shape always fills the height. Each line names the CSS variable it is
	 * drawn in - reading the theme is this component's job, not its caller's.
	 */
	let { labels = [], lines = [] } = $props();

	let canvas;
	let chart = null;

	const cssVar = (name) =>
		getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';

	/** A theme colour at partial opacity; the palette is hex, canvas wants rgba. */
	function fade(hex, opacity) {
		const digits = hex.replace('#', '');
		const full =
			digits.length === 3
				? [...digits].map((c) => c + c).join('')
				: digits.padEnd(6, '0').slice(0, 6);
		const value = parseInt(full, 16);
		return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${opacity})`;
	}

	const monthLabel = (key) => `${formatMonthShort(key)} ${key.slice(0, 4)}`;

	function build() {
		const muted = cssVar('--muted');
		const line = cssVar('--line');

		const datasets = lines.map((series, index) => {
			const colour = cssVar(series.color);
			return {
				label: series.label,
				data: series.values,
				borderColor: colour,
				backgroundColor: fade(colour, 0.12),
				fill: series.fill ? 'origin' : false,
				order: index + 1,
				borderWidth: 2,
				tension: 0.25,
				pointRadius: 0,
				pointHoverRadius: 4,
				pointHitRadius: 12
			};
		});

		chart = new Chart(canvas, {
			type: 'line',
			data: { labels, datasets },
			options: {
				responsive: true,
				maintainAspectRatio: false,
				animation: { duration: 250 },
				interaction: { mode: 'index', intersect: false },
				scales: {
					x: {
						grid: { display: false },
						border: { color: line },
						ticks: {
							color: muted,
							maxRotation: 0,
							autoSkip: true,
							maxTicksLimit: 12,
							callback: (_, index) => monthLabel(labels[index])
						}
					},
					y: {
						beginAtZero: true,
						grid: { color: fade(line, 0.7) },
						border: { display: false },
						ticks: { color: muted, precision: 0, maxTicksLimit: 8 }
					}
				},
				plugins: {
					legend: {
						// One line needs no legend - the heading above already names it.
						display: lines.length > 1,
						align: 'end',
						labels: { color: muted, boxWidth: 10, boxHeight: 10, padding: 14 }
					},
					tooltip: {
						callbacks: {
							title: (items) => monthLabel(labels[items[0].dataIndex]),
							label: (item) => ` ${item.dataset.label}: ${formatCount(item.parsed.y)}`
						}
					}
				}
			}
		});
	}

	$effect(() => {
		// Touch every input so a change in any of them redraws.
		void [labels, lines];
		chart?.destroy();
		if (labels.length && lines.length) build();
		return () => {
			chart?.destroy();
			chart = null;
		};
	});
</script>

<div class="chart">
	<canvas bind:this={canvas}></canvas>
</div>

<style>
	.chart {
		position: relative;
		flex: 1;
		min-height: 0;
	}
</style>
