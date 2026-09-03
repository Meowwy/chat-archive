<script>
	import { mediaUrl } from '$lib/api.js';
	import { formatFull, formatSize, formatTime, linkify } from '$lib/format.js';

	let { message, grouped = false, highlighted = false, onimage } = $props();

	const isImage = (a) => (a.type ?? '').startsWith('image/') && a.sha256;
	const isVideo = (a) => (a.type ?? '').startsWith('video/') && a.sha256;
	const isAudio = (a) => (a.type ?? '').startsWith('audio/') && a.sha256;

	let parts = $derived(linkify(message.text));
	let shares = $derived(message.embeds.filter((e) => e.link || e.url));
</script>

<article
	class:mine={message.is_self}
	class:grouped
	class:highlighted
	id="m{message.message_id}"
	data-mid={message.message_id}
	data-ts={message.timestamp}
>
	{#if !grouped}
		<div class="meta">
			<span class="sender">{message.sender}</span>
			<time title={formatFull(message.timestamp)}>{formatTime(message.timestamp)}</time>
		</div>
	{/if}

	<div class="bubble">
		{#if message.is_unsent}
			<em class="unsent">Message was unsent</em>
		{:else if message.text}
			<p>
				{#each parts as part, i (i)}
					{#if part.type === 'link'}
						<a href={part.value} target="_blank" rel="noreferrer noopener">{part.value}</a>
					{:else}{part.value}{/if}
				{/each}
			</p>
		{/if}

		{#each message.attachments as attachment (attachment.attachment_id)}
			{#if isImage(attachment)}
				<button
					class="thumb"
					onclick={() => onimage?.(attachment)}
					title={attachment.name}
					aria-label={attachment.name}
				>
					<img src={mediaUrl(attachment.sha256)} alt={attachment.name} loading="lazy" />
				</button>
			{:else if isVideo(attachment)}
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={mediaUrl(attachment.sha256)} controls preload="metadata"></video>
			{:else if isAudio(attachment)}
				<audio src={mediaUrl(attachment.sha256)} controls preload="metadata"></audio>
			{:else if attachment.sha256}
				<a class="file" href={mediaUrl(attachment.sha256)} target="_blank" rel="noreferrer">
					<span class="icon">↓</span>
					<span class="fname">{attachment.name}</span>
					<span class="fsize">{formatSize(attachment.size)}</span>
				</a>
			{:else}
				<!-- Discord CDN links expire ~24h after they are issued, so most
				     older attachments can no longer be fetched. Show what we know. -->
				<div class="file dead" title={attachment.normalized_url}>
					<span class="icon">⚠</span>
					<span class="fname">{attachment.name}</span>
					<span class="fsize">
						{formatSize(attachment.size)}{attachment.width
							? ` · ${attachment.width}×${attachment.height}`
							: ''} · file no longer available
					</span>
				</div>
			{/if}
		{/each}

		{#each shares as share, i (i)}
			<a class="share" href={share.link ?? share.url} target="_blank" rel="noreferrer noopener">
				{#if share.title}<strong>{share.title}</strong>{/if}
				{#if share.share_text || share.description}
					<span class="desc">{share.share_text ?? share.description}</span>
				{/if}
				<span class="url">{share.link ?? share.url}</span>
			</a>
		{/each}

		{#if message.reactions.length}
			<div class="reactions">
				{#each message.reactions as reaction (reaction.emoji_name)}
					<span class="reaction" title={reaction.actors ?? ''}>
						{reaction.emoji_name}
						{#if reaction.count > 1}<b>{reaction.count}</b>{/if}
					</span>
				{/each}
			</div>
		{/if}
	</div>
</article>

<style>
	article {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		margin: 10px 0 0;
		max-width: 74%;
	}

	article.grouped {
		margin-top: 2px;
	}

	article.mine {
		align-self: flex-end;
		align-items: flex-end;
	}

	article.highlighted .bubble {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.meta {
		display: flex;
		gap: 8px;
		align-items: baseline;
		font-size: 12px;
		margin-bottom: 3px;
		padding: 0 4px;
	}

	.sender {
		font-weight: 600;
		color: var(--text);
	}

	time {
		color: var(--muted);
	}

	.bubble {
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		padding: 8px 12px;
		min-width: 0;
	}

	article.mine .bubble {
		background: var(--mine);
	}

	p {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.unsent {
		color: var(--muted);
	}

	.thumb {
		display: block;
		padding: 0;
		border: none;
		background: none;
		margin-top: 6px;
	}

	.thumb img,
	video {
		max-width: min(340px, 100%);
		max-height: 340px;
		border-radius: 8px;
		display: block;
	}

	audio {
		margin-top: 6px;
		max-width: 300px;
	}

	.file {
		display: flex;
		max-width: 100%;
		align-items: center;
		gap: 8px;
		margin-top: 6px;
		padding: 7px 10px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 8px;
		text-decoration: none;
		color: inherit;
		font-size: 13px;
	}

	.file.dead {
		opacity: 0.7;
		border-style: dashed;
	}

	.fname {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		/* Without this a 100-character Discord filename refuses to shrink and
		   pushes a horizontal scrollbar across the whole conversation. */
		min-width: 0;
	}

	.fsize {
		color: var(--muted);
		white-space: nowrap;
		font-size: 12px;
	}

	.share {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin-top: 6px;
		padding: 8px 10px;
		border-left: 3px solid var(--accent);
		background: var(--panel-2);
		border-radius: 0 8px 8px 0;
		text-decoration: none;
		color: inherit;
		font-size: 13px;
	}

	.share .desc {
		color: var(--muted);
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.share .url {
		color: var(--accent);
		font-size: 12px;
		overflow-wrap: anywhere;
	}

	.reactions {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
		margin-top: 6px;
	}

	.reaction {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 1px 8px;
		font-size: 13px;
	}

	.reaction b {
		font-size: 11px;
		color: var(--muted);
		margin-left: 3px;
	}
</style>
