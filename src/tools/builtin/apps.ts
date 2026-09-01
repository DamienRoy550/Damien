import type { Tool } from '../types';
import { ok, err } from '../types';

/**
 * App & website launching.
 *
 * - `open_website` opens any URL in the browser (normalizing bare domains).
 * - `open_app` launches installed apps via, in order:
 *     1. an explicit URL scheme the user/model provided ("whatsapp://send"),
 *     2. a curated directory of popular apps ("youtube" → "youtube://"),
 *     3. an Android package name ("com.spotify.music") via an intent: URI,
 *     4. a best-effort guess of "<name>://".
 *
 * Platform reality check (documented honestly):
 *  - Android: any installed app can be launched by package name through an
 *    intent URI. On Android 11+ the app must be visible to the launcher;
 *    hidden apps may need a <queries> entry (see docs/).
 *  - iOS: launching is only possible for apps that register a URL scheme —
 *    hence the directory. Unknown apps on iOS fail with a clear message.
 *  - Web demo: opens a new browser tab for websites (allow popups); app
 *    schemes generally have nothing to handle them and error out honestly.
 */

/** Popular apps → URL scheme (scheme without trailing "://"). Best effort. */
export const APP_SCHEMES: Record<string, string> = {
  youtube: 'youtube',
  ytmusic: 'youtubemusic',
  'youtube music': 'youtubemusic',
  whatsapp: 'whatsapp',
  spotify: 'spotify',
  facebook: 'fb',
  fb: 'fb',
  messenger: 'fb-messenger',
  instagram: 'instagram',
  twitter: 'twitter',
  x: 'twitter',
  telegram: 'tg',
  tiktok: 'snssdk1233',
  snapchat: 'snapchat',
  signal: 'sgnl',
  reddit: 'reddit',
  discord: 'discord',
  slack: 'slack',
  zoom: 'zoomus',
  teams: 'msteams',
  gmail: 'googlegmail',
  outlook: 'ms-outlook',
  googlemaps: 'comgooglemaps',
  'google maps': 'comgooglemaps',
  maps: 'maps',
  waze: 'waze',
  uber: 'uber',
  lyft: 'lyft',
  netflix: 'nflx',
  primevideo: 'aiv',
  amazon: 'amazon',
  paypal: 'paypal',
  venmo: 'venmo',
  linkedin: 'linkedin',
  pinterest: 'pinterest',
  twitch: 'twitch',
  github: 'github',
  chrome: 'googlechrome',
  firefox: 'firefox',
  shazam: 'shazam',
  duolingo: 'duolingo',
  settings: 'app-settings',
};

/**
 * Web equivalents so the browser demo can "open" popular apps even though
 * schemes cannot launch inside a web page. Native devices use the schemes
 * above; the demo falls back to these.
 */
export const APP_WEB_FALLBACKS: Record<string, string> = {
  youtube: 'https://www.youtube.com',
  ytmusic: 'https://music.youtube.com',
  'youtube music': 'https://music.youtube.com',
  whatsapp: 'https://web.whatsapp.com',
  spotify: 'https://open.spotify.com',
  facebook: 'https://www.facebook.com',
  fb: 'https://www.facebook.com',
  messenger: 'https://www.messenger.com',
  instagram: 'https://www.instagram.com',
  twitter: 'https://x.com',
  x: 'https://x.com',
  telegram: 'https://web.telegram.org',
  tiktok: 'https://www.tiktok.com',
  reddit: 'https://www.reddit.com',
  discord: 'https://discord.com/app',
  slack: 'https://app.slack.com',
  gmail: 'https://mail.google.com',
  outlook: 'https://outlook.live.com',
  googlemaps: 'https://www.google.com/maps',
  'google maps': 'https://www.google.com/maps',
  maps: 'https://www.google.com/maps',
  waze: 'https://www.waze.com/live-map',
  uber: 'https://m.uber.com',
  netflix: 'https://www.netflix.com',
  amazon: 'https://www.amazon.com',
  paypal: 'https://www.paypal.com',
  linkedin: 'https://www.linkedin.com',
  pinterest: 'https://www.pinterest.com',
  twitch: 'https://www.twitch.tv',
  github: 'https://github.com',
  chrome: 'https://www.google.com/chrome',
  shazam: 'https://www.shazam.com',
  duolingo: 'https://www.duolingo.com',
};

const ANDROID_PACKAGE_RE = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,}$/i;
const KNOWN_TLD_RE =
  /\.(com|net|org|io|co|dev|app|ai|edu|gov|info|me|tv|xyz|uk|us|ca|de|fr|es|it|nl|se|no|fi|jp|kr|in|au|br|mx|ru|ch|fm|gg|so|to|sh|is|be|at|pt|pl|cz|ie|nz|za|ng|ke|fj)$/i;
const EXPLICIT_SCHEME_RE = /^[a-z][a-z0-9+.-]*:(\/\/)?/i;

/** True when the text looks like a website rather than an app reference. */
export function looksLikeWebsite(text: string): boolean {
  const t = text.trim();
  if (/^https?:\/\//i.test(t) || /^www\./i.test(t)) return true;
  // The LAST dot-segment decides: "youtube.com" → website,
  // "com.whatsapp" / "com.spotify.music" → package (last segment not a TLD).
  if (/\./.test(t) && KNOWN_TLD_RE.test(t)) return true;
  return false;
}

function slug(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]/g, '');
}

/** Build the URL open_app will try for a given input. Exported for tests. */
export function resolveAppTarget(input: string): {
  url: string;
  strategy: string;
  webFallback?: string;
} {
  const t = input.trim();
  const key = APP_SCHEMES[t.toLowerCase()] !== undefined ? t.toLowerCase() : slug(t);

  // 1. Explicit scheme / deep link ("whatsapp://send?text=hi")
  if (EXPLICIT_SCHEME_RE.test(t) && /:\/\//.test(t)) {
    return { url: t, strategy: 'deep link' };
  }

  // 2. Curated alias ("youtube" → "youtube://"), with a web fallback for demos
  const alias = APP_SCHEMES[key];
  if (alias) {
    return {
      url: `${alias}://`,
      strategy: `app shortcut "${t}"`,
      webFallback: APP_WEB_FALLBACKS[key],
    };
  }

  // 3. Android package ("com.spotify.music" → intent URI)
  if (ANDROID_PACKAGE_RE.test(t) && !KNOWN_TLD_RE.test(t)) {
    return {
      url: `intent:#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;package=${t};end`,
      strategy: 'Android package',
    };
  }

  // 4. Best-effort guess
  return { url: `${slug(t)}://`, strategy: 'guessed scheme' };
}

/** Open any website in the browser. */
export const openWebsite: Tool = {
  name: 'open_website',
  description:
    'Open a website in the phone browser. Accepts full URLs ("https://x.com") or bare domains ("youtube.com"). Use when the user says open/visit/go to a site.',
  parameters: [
    { name: 'url', type: 'string', description: 'The website to open, e.g. "en.wikipedia.org/wiki/Cat"', required: true },
  ],
  runsOffline: true,
  async execute(args, ctx) {
    if (!ctx.device) return err('Opening websites is not available in this environment.');
    let url = String(args.url ?? args.website ?? args.link ?? '').trim();
    if (!url) return err('url is required');
    if (!/^https?:\/\//i.test(url)) {
      if (!/^[\w-]+(\.[\w-]+)+/.test(url)) {
        return err(`"${url}" does not look like a website. Include a domain, e.g. "example.com".`);
      }
      url = `https://${url}`;
    }
    try {
      if (ctx.device.openInAppBrowser) {
        await ctx.device.openInAppBrowser(url);
        return ok(`Opened ${url} in the in-app browser. Confirm briefly to the user.`);
      }
      await ctx.device.openUrl(url);
      return ok(`Opened ${url} in the browser. Confirm briefly to the user.`);
    } catch (e) {
      return err(`Could not open ${url}: ${e instanceof Error ? e.message : String(e)}`);
    }
  },
};

/** Launch an installed app by name, scheme, or Android package. */
export const openApp: Tool = {
  name: 'open_app',
  description:
    'Open/launch an installed app on the phone. Works with popular app names ("youtube", "whatsapp", "spotify"), Android package names ("com.spotify.music"), or any deep link ("whatsapp://send"). Use when the user says open/launch/start an app.',
  parameters: [
    {
      name: 'app',
      type: 'string',
      description:
        'The app to open: a well-known name ("youtube"), an Android package ("com.whatsapp"), or a deep link ("spotify://playlist/abc")',
      required: true,
    },
  ],
  runsOffline: true,
  async execute(args, ctx) {
    if (!ctx.device) return err('Opening apps is not available in this environment.');
    const input = String(args.app ?? args.name ?? args.package ?? args.scheme ?? '').trim();
    if (!input) return err('app is required');

    const { url, strategy, webFallback } = resolveAppTarget(input);
    try {
      await ctx.device.openUrl(url);
      return ok(`Opened ${input} (${strategy}). Confirm briefly to the user.`);
    } catch {
      // Web demo (or scheme-less platform): open the app's web version instead.
      if (webFallback && ctx.device.openInAppBrowser) {
        try {
          await ctx.device.openInAppBrowser(webFallback);
          return ok(
            `Opened the web version of ${input} at ${webFallback} (app launching needs the phone app). Tell the user this.`,
          );
        } catch {
          // fall through to the error below
        }
      }
      if (strategy === 'Android package') {
        return err(
          `No app responded for package "${input}". It may not be installed. You can offer to open its store page with open_website url "play.google.com/store/apps/details?id=${input}".`,
        );
      }
      return err(
        `No installed app handled "${input}" (tried ${url}). On iOS only apps with a URL scheme can be launched — try the app's package name on Android, or a full deep link.`,
      );
    }
  },
};
