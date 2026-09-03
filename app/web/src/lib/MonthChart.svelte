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
	 * Messages per month, as a line over the whole timeline.
	 *
	 * `months` is whatever is being counted - every message, or only the ones
	 * that used a word - with its gaps filled, so a silent month is a zero
	 * rather than a straight line across it. Whichever it is, it is the only
	 * thing plotted and it owns the y-axis, so the shape fills the height.
	 */
	let {
		months = [],
		split = false,
		names = { mine: 'me', theirs: 'them' },
		label = 'messages'
	} = $props();

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
		const accent = cssVar('--accent');
		const me = cssVar('--me');
		const them = cssVar('--them');

		const labels = months.map((m) => m.month);
		const stroke = {
			borderWidth: 2,
			tension: 0.25,
			pointRadius: 0,
			pointHoverRadius: 4,
			pointHitRadius: 12
		};

		const datasets = [];
		if (split) {
			datasets.push(
				{
					label: names.mine,
					data: months.map((m) => m.mine),
					borderColor: me,
					backgroundColor: fade(me, 0.12),
					fill: 'origin',
					order: 1,
					...stroke
				},
				{
					label: names.theirs,
					data: months.map((m) => m.theirs),
					borderColor: them,
					backgroundColor: fade(them, 0.12),
					fill: 'origin',
					order: 2,
					...stroke
				}
			);
		} else {
			datasets.push({
				label,
				data: months.map((m) => m.messages),
				borderColor: accent,
				backgroundColor: fade(accent, 0.14),
				fill: 'origin',
				order: 1,
				...stroke
			});
		}

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
						display: split,
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
		void [months, split, names, label];
		chart?.destroy();
		if (months.length) build();
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
