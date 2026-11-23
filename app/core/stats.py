# -*- coding: utf-8 -*-
"""互动数据：页面上下文 API 抓取（自带登录态+签名）。"""
import json
import os

from paths import BASE

API_SCRIPT = r"""
async (payload) => {
    const results = {videos: {}, author: null};
    const headers = {'Referer': 'https://www.douyin.com/'};
    for (const vid of payload.video_ids) {
        try {
            const url = '/aweme/v1/web/aweme/detail/?aweme_id=' + vid +
                '&device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1';
            const r = await fetch(url, {headers});
            const d = await r.json();
            const a = d.aweme_detail;
            if (a) {
                const s = a.statistics || {};
                results.videos[vid] = {
                    like: s.digg_count ?? '-', comment: s.comment_count ?? '-',
                    collect: s.collect_count ?? '-', share: s.share_count ?? '-',
                    play: s.play_count ?? '-',
                    author_name: (a.author && a.author.nickname) || '',
                    author_id: (a.author && a.author.sec_uid) || '',
                };
            } else {
                results.videos[vid] = {error: (d.status_msg || 'no data').slice(0, 50)};
            }
        } catch (e) {
            results.videos[vid] = {error: String(e).slice(0, 50)};
        }
    }
    if (payload.sec_user_id) {
        try {
            const url = '/aweme/v1/web/user/profile/other/?sec_user_id=' + payload.sec_user_id +
                '&device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1';
            const r = await fetch(url, {headers});
            const d = await r.json();
            const u = d.user;
            if (u) {
                results.author = {
                    nickname: u.nickname || '', fans: u.follower_count ?? '-',
                    following: u.following_count ?? '-', total_likes: u.total_favorited ?? '-',
                    works: u.aweme_count ?? '-',
                };
            }
        } catch (e) {
            results.author = {error: String(e).slice(0, 50)};
        }
    }
    return results;
}
"""


def fetch(video_ids, sec_user_id=None):
    if not video_ids and not sec_user_id:
        return {"videos": {}, "author": None}
    try:
        import accounts
        import browser
        sb = browser.shared()
        ctx, pg = sb.new_page(accounts.profile_dir(), headless=True,
                              args=browser.LAUNCH_ARGS)
        try:
            # 先打开视频页建立登录态与签名上下文（直接调 API 会被风控拦截）
            if video_ids:
                try:
                    pg.goto(f"https://www.douyin.com/video/{video_ids[0]}",
                            wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pg.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            else:
                pg.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(3500)
            res = pg.evaluate(API_SCRIPT, {"video_ids": video_ids, "sec_user_id": sec_user_id})
            # 风控拦截重试一次（多等一会，等页面完整初始化）
            bad = [k for k, v in (res.get("videos") or {}).items()
                   if isinstance(v, dict) and v.get("error")]
            if bad and video_ids:
                pg.wait_for_timeout(4000)
                res2 = pg.evaluate(API_SCRIPT, {"video_ids": video_ids, "sec_user_id": sec_user_id})
                for k, v in (res2.get("videos") or {}).items():
                    if not (isinstance(v, dict) and v.get("error")):
                        res["videos"][k] = v
            return res
        finally:
            sb.close_page(pg)
    except Exception as e:
        print(f"[stats] 抓取失败: {str(e)[:120]}", flush=True)
        return {"videos": {}, "author": None, "error": str(e)[:100]}


def one(video_id):
    r = fetch([video_id])
    return r.get("videos", {}).get(video_id, {}) or {}
