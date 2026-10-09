// Independent public-protocol adapter. No third-party implementation copied.
const MAX_BODY = 2_000_000;
const TIMEOUT_MS = 6000;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36';

export function shortcode(input) {
  try {
    const u = new URL(input);
    const match = /^\/(?:reel|p)\/([A-Za-z0-9_-]{1,28})\/?$/.exec(u.pathname);
    if (u.protocol === 'https:' && ['instagram.com', 'www.instagram.com'].includes(u.hostname)
        && !u.username && !u.password && !u.port && match) return match[1];
  } catch { /* Fixed diagnostic at caller. */ }
  throw new Error('invalid_instagram_url');
}

export function allowedMedia(input) {
  if (typeof input !== 'string' || input.length > 16384) return false;
  try {
    const u = new URL(input);
    return u.protocol === 'https:' && !u.username && !u.password && !u.port
      && ['.cdninstagram.com', '.fbcdn.net'].some(s => u.hostname.endsWith(s));
  } catch { return false; }
}

function metadata(node, code) {
  if (!node || typeof node !== 'object' || (node.code || node.shortcode) !== code
      || node.user?.is_private || node.owner?.is_private) throw new Error('identity_or_privacy');
  const versions = node.video_versions || [{url: node.video_url,
    width: node.dimensions?.width, height: node.dimensions?.height}];
  if (!Array.isArray(versions)) throw new Error('invalid_metadata');
  const valid = versions.filter(v => v && allowedMedia(v.url)
    && [v.width, v.height].every(n => n == null || (Number.isInteger(n) && n > 0 && n <= 4096)));
  valid.sort((a, b) => (b.width || 0) * (b.height || 0) - (a.width || 0) * (a.height || 0));
  if (!valid.length) throw new Error('no_video');
  const duration = node.video_duration;
  return {shortcode: code, video_url: valid[0].url,
    width: valid[0].width ?? null, height: valid[0].height ?? null,
    duration: typeof duration === 'number' && Number.isFinite(duration) && duration > 0 ? duration : null};
}

function fromPayload(payload, code) {
  const data = payload?.data;
  if (!data || typeof data !== 'object') {
    const codes = (Array.isArray(payload?.errors) ? payload.errors : [])
      .filter(e => Number.isSafeInteger(e?.code) && e.code >= 0)
      .slice(0, 3).map(e => e.code);
    throw new Error(codes.length ? `errors_${codes.join('_')}` : 'data_missing');
  }
  return metadata(data.xdt_api__v1__media__shortcode__web_info?.items?.[0]
    || data.xdt_shortcode_media || data.shortcode_media, code);
}

// Read one balanced JSON object, respecting escaped quotes and braces in strings.
function objectAt(text, start) {
  while (/\s/.test(text[start] || '') && start < text.length) start++;
  if (text[start] !== '{') throw new Error('parse_failed');
  let depth = 0, quoted = false, escaped = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === '"') quoted = false;
    } else if (c === '"') quoted = true;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return JSON.parse(text.slice(start, i + 1));
  }
  throw new Error('parse_failed');
}

export function parsePost(text, code) {
  const match = /"xig_polaris_media"\s*:\s*/.exec(text);
  if (!match) throw new Error('media_missing');
  const wrapper = objectAt(text, match.index + match[0].length);
  const node = 'if_not_gated_logged_out' in wrapper ? wrapper.if_not_gated_logged_out : wrapper;
  return metadata(node, code);
}

export function parseEmbed(text, code) {
  const pattern = /"init",\s*\[\s*\],\s*\[/g;
  let match, count = 0;
  while ((match = pattern.exec(text)) && count++ < 20) {
    let context;
    try {
      const init = objectAt(text, pattern.lastIndex);
      context = typeof init.contextJSON === 'string' ? JSON.parse(init.contextJSON) : init.contextJSON;
    } catch { continue; }
    if (context && typeof context === 'object') {
      return context.gql_data ? fromPayload({data: context.gql_data}, code) : metadata(context, code);
    }
  }
  throw new Error('parse_failed');
}

async function upstream(url, init, fetcher) {
  // Destinations are fixed by the three stages below; redirects never get followed.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetcher(url, {...init, redirect: 'manual', signal: controller.signal});
    if (response.status !== 200) {
      let reason = `http_${response.status}`;
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        try {
          const target = new URL(response.headers.get('Location'), url);
          if (target.hostname === 'www.instagram.com' && target.pathname.startsWith('/accounts/login')) {
            reason = 'redirect_login';
          }
        } catch { /* Do not fetch Location. */ }
      }
      await response.body?.cancel();
      throw new Error(reason);
    }
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BODY) throw new Error('body_too_large');
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return new TextDecoder().decode(bytes);
  } catch (error) {
    if (controller.signal.aborted) throw new Error('timeout');
    throw error;
  } finally { clearTimeout(timer); }
}

const SAFE_REASON = /^(?:http_[1-5][0-9]{2}|errors_(?:[0-9]+_?){1,3}|redirect_login|timeout|body_too_large|identity_or_privacy|invalid_metadata|no_video|data_missing|parse_failed|media_missing|token_missing|not_json)$/;

export async function resolve(code, fetcher = fetch) {
  const headers = {'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'};
  const post = `https://www.instagram.com/p/${code}/`;
  const stages = [
    ['embed', async () => parseEmbed(await upstream(`${post}embed/captioned/`, {headers}, fetcher), code)],
    ['post_html', async () => parsePost(await upstream(post, {headers: {...headers,
      'User-Agent': 'Googlebot/2.1 (+http://www.google.com/bot.html)'}}, fetcher), code)],
    ['graphql_post_root_271', async () => {
      const home = await upstream('https://www.instagram.com/', {headers}, fetcher);
      const lsd = /\["LSD",\[\],\{"token":"([^"]+)"/.exec(home)?.[1];
      if (!lsd) throw new Error('token_missing');
      const body = new URLSearchParams({lsd, doc_id: '27128499623469141',
        server_timestamps: 'true', variables: JSON.stringify({shortcode: code,
          __relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider: false})});
      const raw = await upstream('https://www.instagram.com/graphql/query', {method: 'POST',
        headers: {...headers, 'X-FB-LSD': lsd, 'X-IG-App-ID': '936619743392459',
          'X-FB-Friendly-Name': 'PolarisPostRootQuery', 'Content-Type': 'application/x-www-form-urlencoded'}, body}, fetcher);
      let payload;
      try { payload = JSON.parse(raw); } catch { throw new Error('not_json'); }
      return fromPayload(payload, code);
    }],
  ];
  const attempts = {};
  for (const [source, operation] of stages) {
    try { return {ok: true, source, ...await operation()}; }
    catch (error) { attempts[source] = SAFE_REASON.test(error?.message) ? error.message : 'transport_or_unexpected_error'; }
  }
  return {ok: false, reason: 'public_methods_exhausted', attempts};
}

async function authenticated(provided, secret) {
  if (typeof secret !== 'string' || secret.length < 32 || secret.length > 256
      || typeof provided !== 'string' || provided.length > 256) return false;
  const encode = new TextEncoder();
  const a = new Uint8Array(await crypto.subtle.digest('SHA-256', encode.encode(provided)));
  const b = new Uint8Array(await crypto.subtle.digest('SHA-256', encode.encode(secret)));
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export async function handle(request, env, fetcher = fetch) {
  const reply = (body, status) => Response.json(body, {status,
    headers: {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}});
  const url = new URL(request.url);
  if (url.pathname !== '/instagram') return reply({ok: false, reason: 'not_found'}, 404);
  if (request.method !== 'GET') return reply({ok: false, reason: 'method_not_allowed'}, 405);
  if (!await authenticated(request.headers.get('X-ReCut-Resolver-Secret'), env.INSTAGRAM_RESOLVER_SECRET)) {
    return reply({ok: false, reason: 'unauthorized'}, 401);
  }
  let code;
  try {
    if (url.searchParams.getAll('url').length !== 1) throw new Error();
    code = shortcode(url.searchParams.get('url'));
  } catch { return reply({ok: false, reason: 'invalid_instagram_url'}, 400); }
  const result = await resolve(code, fetcher);
  return reply(result, result.ok ? 200 : 502);
}

export default {async fetch(request, env) {
  try { return await handle(request, env); }
  catch { return Response.json({ok: false, reason: 'internal_error'}, {status: 500,
    headers: {'Cache-Control': 'no-store'}}); }
}};
