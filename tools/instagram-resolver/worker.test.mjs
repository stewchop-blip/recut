import test from 'node:test';
import assert from 'node:assert/strict';
import {handle, resolve, shortcode, allowedMedia, parsePost, parseEmbed} from './worker.mjs';

const secret = 'unit-test-only-' + 'x'.repeat(32);
const env = {INSTAGRAM_RESOLVER_SECRET: secret};
const video = 'https://video.cdninstagram.com/v.mp4?signature=NEVER_LOG_ME';
const node = {code: 'abc', user: {is_private: false}, video_duration: 4,
  video_versions: [{url: video, width: 720, height: 1280}]};
const embed = value => '["init",[],[' + JSON.stringify({contextJSON: JSON.stringify(value)}) + ']]';
const post = value => '"xig_polaris_media":' + JSON.stringify({if_not_gated_logged_out: value});
const request = (input = 'https://www.instagram.com/reel/abc/', auth = secret) =>
  new Request('https://resolver.workers.dev/instagram?url=' + encodeURIComponent(input),
    {headers: {'X-ReCut-Resolver-Secret': auth}});

for (const input of ['http://www.instagram.com/reel/abc/', 'https://127.0.0.1/reel/abc/',
  'https://instagram.com.evil.test/reel/abc/', 'https://user@instagram.com/reel/abc/',
  'https://instagram.com:8443/reel/abc/', 'https://instagram.com/stories/abc/',
  'https://instagram.com/reel/abc/extra', 'https://instagram.com/reel/abc%2F../']) {
  test('reject input ' + input, () => assert.throws(() => shortcode(input)));
}
for (const input of ['http://video.fbcdn.net/v', 'https://127.0.0.1/v',
  'https://cdninstagram.com.evil.test/v', 'https://evilfbcdn.net/v',
  'https://user@video.fbcdn.net/v', 'https://video.fbcdn.net:8443/v', null]) {
  test('reject media ' + input, () => assert.equal(allowedMedia(input), false));
}
test('accept only canonical source and CDN hosts', () => {
  assert.equal(shortcode('https://instagram.com/p/abc/?igsh=tracking'), 'abc');
  assert.equal(allowedMedia(video), true);
  assert.equal(allowedMedia('https://scontent.foo.fbcdn.net/v'), true);
});
test('balanced HTML JSON and highest quality', () => {
  const value = {...node, caption: {text: 'braces } and " quotes'}, video_versions: [
    {url: video, width: 360, height: 640}, {url: video, width: 720, height: 1280},
    {url: 'https://evil.test/v', width: 4096, height: 4096}]};
  assert.equal(parsePost(post(value), 'abc').height, 1280);
  assert.equal(parseEmbed(embed({gql_data: {shortcode_media: {
    shortcode: 'abc', video_url: video, owner: {}, video_duration: 4}}}), 'abc').video_url, video);
});
test('reject mismatched/private/gated metadata', () => {
  for (const value of [null, {...node, code: 'other'}, {...node, user: {is_private: true}},
    {...node, video_versions: [{url: 'https://localhost/v'}]}]) {
    assert.throws(() => parsePost(post(value), 'abc'));
  }
});
test('resolver success returns no extra upstream fields', async () => {
  const response = await handle(request(), env, async (url, options) => {
    assert.equal(options.redirect, 'manual');
    assert.ok(url.endsWith('/embed/captioned/'));
    assert.equal(options.headers['X-ReCut-Resolver-Secret'], undefined);
    return new Response(embed({...node, unrelated: secret}));
  });
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.source, 'embed');
  assert.equal(data.video_url, video);
  assert.equal(data.unrelated, undefined);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
});
test('embed failure then post success', async () => {
  let calls = 0;
  const data = await resolve('abc', async () => ++calls === 1
    ? new Response(null, {status: 302, headers: {Location: 'https://evil.test/'}})
    : new Response(post(node)));
  assert.equal(calls, 2);
  assert.equal(data.source, 'post_html');
});
test('GraphQL last; HTTP failures and numeric diagnostics only', async () => {
  let calls = 0;
  const data = await resolve('abc', async (url, options) => {
    calls++;
    if (calls <= 2) return new Response(null, {status: 401});
    if (calls === 3) return new Response('["LSD",[],{"token":"anonymous-test"}]');
    assert.equal(url, 'https://www.instagram.com/graphql/query');
    assert.equal(options.body.get('doc_id'), '27128499623469141');
    return Response.json({data: null, errors: [{code: 1675004, message: video + secret}]});
  });
  assert.equal(calls, 4);
  assert.deepEqual(data.attempts, {embed: 'http_401', post_html: 'http_401',
    graphql_post_root_271: 'errors_1675004'});
  assert.ok(!JSON.stringify(data).includes(secret));
});
test('GraphQL success', async () => {
  let calls = 0;
  const data = await resolve('abc', async () => {
    calls++;
    if (calls <= 2) return new Response('no media');
    if (calls === 3) return new Response('["LSD",[],{"token":"anonymous-test"}]');
    return Response.json({data: {xdt_shortcode_media: {shortcode: 'abc', video_url: video}}});
  });
  assert.equal(data.source, 'graphql_post_root_271');
});
test('invalid JSON has fixed reason', async () => {
  let calls = 0;
  const data = await resolve('abc', async () => {
    calls++;
    return new Response(calls === 3 ? '["LSD",[],{"token":"anonymous-test"}]' : '{invalid');
  });
  assert.equal(data.attempts.graphql_post_root_271, 'not_json');
});
test('timeout aborts stage and continues to existing public route', async () => {
  let calls = 0;
  const originalTimer = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms) => originalTimer(fn, Math.min(ms, 5));
  try {
    const data = await resolve('abc', async (_url, {signal}) => {
      if (++calls === 1) return new Promise((_resolve, reject) =>
        signal.addEventListener('abort', () => reject(new Error(video))));
      return new Response(post(node));
    });
    assert.equal(data.source, 'post_html');
    assert.equal(calls, 2);
  } finally { globalThis.setTimeout = originalTimer; }
});
test('size cap and no signed URLs in diagnostics or console logs', async () => {
  const original = console.log;
  const logs = [];
  console.log = (...args) => logs.push(args);
  try {
    const data = await resolve('abc', async () => { throw new Error(video + secret); });
    assert.ok(!JSON.stringify({data, logs}).includes('NEVER_LOG_ME'));
    assert.ok(!JSON.stringify({data, logs}).includes(secret));
    const large = await resolve('abc', async () => new Response('x'.repeat(2_000_001)));
    assert.equal(large.attempts.embed, 'body_too_large');
  } finally { console.log = original; }
});
test('missing secret / invalid auth / invalid URL never make network requests', async () => {
  const forbidden = () => assert.fail('network must not be called');
  assert.equal((await handle(request(), {}, forbidden)).status, 401);
  assert.equal((await handle(request(undefined, 'wrong'), env, forbidden)).status, 401);
  assert.equal((await handle(request('https://127.0.0.1/p/abc'), env, forbidden)).status, 400);
  assert.equal((await handle(new Request('https://resolver.workers.dev/video'), env, forbidden)).status, 404);
});
