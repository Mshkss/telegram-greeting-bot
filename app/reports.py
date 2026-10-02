from datetime import date, datetime, time, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))


def date_range(query, now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(MOSCOW).date()
    try:
        end = date.fromisoformat(query.get('to', today.isoformat()))
        start = date.fromisoformat(query.get('from', (end-timedelta(days=29)).isoformat()))
    except (ValueError, TypeError):
        raise ValueError('Укажите даты в формате ГГГГ-ММ-ДД.')
    if start > end or end > today or (end-start).days > 365:
        raise ValueError('Период: от 1 до 366 дней, без будущих дат.')
    return start, end


async def dashboard_report(db, start, end, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MOSCOW).date()
    lo = datetime.combine(start, time.min, MOSCOW)
    hi = datetime.combine(end+timedelta(days=1), time.min, MOSCOW)
    report = {'start': start, 'end': end, 'today': today}
    async with db.pool.acquire() as c:
        async with c.transaction(isolation='repeatable_read', readonly=True):
            report['tracking_since'] = await c.fetchval('SELECT applied_at FROM schema_migrations WHERE version=3')
            report['users'] = await c.fetchrow("""SELECT
                count(*) FILTER(WHERE first_seen IS NOT NULL) AS observed,
                count(*) FILTER(WHERE known_before_tracking) AS legacy,
                count(*) FILTER(WHERE NOT known_before_tracking AND first_seen >= $1 AND first_seen < $2) AS new,
                count(*) FILTER(WHERE first_seen IS NOT NULL AND first_source IS NULL) AS unknown_source
                FROM analytics_users""", lo, hi)
            report['activity'] = await c.fetchrow("""SELECT
                count(DISTINCT user_id) FILTER(WHERE day=$2) AS dau,
                count(DISTINCT user_id) FILTER(WHERE day BETWEEN $2-6 AND $2) AS wau,
                count(DISTINCT user_id) FILTER(WHERE day BETWEEN $2-29 AND $2) AS mau,
                count(DISTINCT user_id) FILTER(WHERE day BETWEEN $1 AND $2) AS period
                FROM analytics_activity_days WHERE day BETWEEN least($1,$2-29) AND $2""", start, end)
            report['sessions'] = await c.fetchrow("""SELECT count(*) AS total,
                count(*) FILTER(WHERE actions=1) AS single_action,
                coalesce(avg(actions),0) AS avg_actions,
                coalesce(avg(extract(epoch FROM last_activity-started_at)),0) AS avg_seconds
                FROM analytics_sessions WHERE started_at >= $1 AND started_at < $2""", lo, hi)
            report['daily'] = await c.fetch("""WITH days AS (
                SELECT generate_series($1::date,$2::date,'1 day')::date AS day
                ), activity AS (
                SELECT day,count(*) AS active FROM analytics_activity_days WHERE day BETWEEN $1 AND $2 GROUP BY day
                ), newcomers AS (
                SELECT (first_seen AT TIME ZONE 'Europe/Moscow')::date AS day,count(*) AS n
                FROM analytics_users WHERE NOT known_before_tracking AND first_seen >= $3 AND first_seen < $4 GROUP BY 1)
                SELECT d.day,coalesce(a.active,0) AS active,coalesce(n.n,0) AS new
                FROM days d LEFT JOIN activity a USING(day) LEFT JOIN newcomers n USING(day) ORDER BY d.day""", start, end, lo, hi)
            rows = await c.fetch("""SELECT name,count(*) AS total,count(DISTINCT user_id) AS users
                FROM analytics_events WHERE occurred_at >= $1 AND occurred_at < $2 GROUP BY name""", lo, hi)
            report['events'] = {r['name']: dict(r) for r in rows}
            report['sources'] = await c.fetch("""WITH starts AS (
                SELECT properties->>'source' AS source,count(*) AS starts,count(DISTINCT user_id) AS visitors
                FROM analytics_events WHERE name IN ('start_first','start_repeat')
                AND occurred_at >= $1 AND occurred_at < $2 GROUP BY 1
                ), newcomers AS (
                SELECT coalesce(first_source,'unknown') AS source,count(*) AS new_users
                FROM analytics_users WHERE NOT known_before_tracking AND first_seen >= $1 AND first_seen < $2 GROUP BY 1)
                SELECT coalesce(s.source,n.source) AS source,coalesce(s.starts,0) AS starts,
                coalesce(s.visitors,0) AS visitors,coalesce(n.new_users,0) AS new_users
                FROM starts s FULL JOIN newcomers n USING(source) ORDER BY new_users DESC,starts DESC,source""", lo, hi)
            report['funnel'] = await c.fetchrow("""SELECT count(*) AS started,
                count(*) FILTER(WHERE 'consent'=ANY(completed_steps)) AS consent,
                count(*) FILTER(WHERE 'gender'=ANY(completed_steps)) AS gender,
                count(*) FILTER(WHERE 'age'=ANY(completed_steps)) AS age,
                count(*) FILTER(WHERE 'photo'=ANY(completed_steps)) AS photo,
                count(*) FILTER(WHERE 'description'=ANY(completed_steps)) AS description,
                count(*) FILTER(WHERE 'goals'=ANY(completed_steps)) AS goals,
                count(*) FILTER(WHERE status='published') AS published,
                count(*) FILTER(WHERE status='cancelled') AS cancelled,
                count(*) FILTER(WHERE status='restarted') AS restarted,
                count(*) FILTER(WHERE status='deleted') AS deleted,
                count(*) FILTER(WHERE status='in_progress' AND last_activity < $3::timestamptz-interval '24 hours') AS stalled
                FROM analytics_attempts WHERE NOT is_edit AND NOT legacy AND started_at >= $1 AND started_at < $2""", lo, hi, now)
            report['dropoffs'] = await c.fetch("""SELECT last_step,count(*) AS n
                FROM analytics_attempts WHERE NOT is_edit AND NOT legacy
                AND started_at >= $1 AND started_at < $2 AND status='in_progress'
                AND last_activity < $3::timestamptz-interval '24 hours' GROUP BY last_step ORDER BY n DESC""", lo, hi, now)
            report['retention'] = await c.fetch("""WITH cohorts AS (
                SELECT user_id,(first_seen AT TIME ZONE 'Europe/Moscow')::date AS day
                FROM analytics_users WHERE NOT known_before_tracking AND first_seen >= $1 AND first_seen < $2
                ) SELECT day,count(*) AS users,
                CASE WHEN day+1 < $3 THEN count(*) FILTER(WHERE EXISTS(
                    SELECT 1 FROM analytics_activity_days a WHERE a.user_id=cohorts.user_id AND a.day=cohorts.day+1)) END AS d1,
                CASE WHEN day+7 < $3 THEN count(*) FILTER(WHERE EXISTS(
                    SELECT 1 FROM analytics_activity_days a WHERE a.user_id=cohorts.user_id AND a.day=cohorts.day+7)) END AS d7
                FROM cohorts GROUP BY day ORDER BY day DESC""", lo, hi, today)
            report['updates'] = await c.fetchrow("""SELECT count(*) AS total,
                count(*) FILTER(WHERE status='error') AS errors,
                count(*) FILTER(WHERE status='processing') AS processing,
                percentile_cont(0.5) WITHIN GROUP(ORDER BY duration_ms) AS p50,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) AS p95
                FROM analytics_updates WHERE received_at >= $1 AND received_at < $2""", lo, hi)
            report['action_times'] = await c.fetch("""SELECT action,count(*) AS total,
                count(*) FILTER(WHERE status='error') AS errors,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) AS p95
                FROM analytics_updates WHERE received_at >= $1 AND received_at < $2 GROUP BY action ORDER BY total DESC""", lo, hi)
            report['telegram'] = await c.fetch("""SELECT properties->>'method' AS method,properties->>'channel' AS channel,
                count(*) AS total,count(*) FILTER(WHERE properties->>'ok'='false') AS failures,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY (properties->>'duration_ms')::double precision) AS p95
                FROM analytics_events WHERE name='telegram_call' AND occurred_at >= $1 AND occurred_at < $2
                GROUP BY 1,2 ORDER BY failures DESC,total DESC""", lo, hi)
            report['errors'] = await c.fetch("""SELECT * FROM (
                SELECT received_at AS at,user_id,'handler' AS kind,error_type FROM analytics_updates
                WHERE received_at >= $1 AND received_at < $2 AND status='error'
                UNION ALL SELECT occurred_at,user_id,properties->>'method',properties->>'error_type'
                FROM analytics_events WHERE name='telegram_call' AND properties->>'ok'='false'
                AND occurred_at >= $1 AND occurred_at < $2
                ) e ORDER BY at DESC LIMIT 30""", lo, hi)
            report['activation_seconds'] = await c.fetchval("""SELECT percentile_cont(0.5)
                WITHIN GROUP(ORDER BY extract(epoch FROM first_published_at-first_seen))
                FROM analytics_users WHERE NOT known_before_tracking AND first_seen >= $1 AND first_seen < $2
                AND first_published_at >= first_seen AND first_published_at < $2""", lo, hi)
            report['matched_active_users'] = await c.fetchval("""WITH matched AS (
                SELECT user_id FROM analytics_events WHERE name='match_created' AND occurred_at >= $1 AND occurred_at < $2
                UNION SELECT (properties->>'target_id')::bigint FROM analytics_events
                WHERE name='match_created' AND occurred_at >= $1 AND occurred_at < $2
                ) SELECT count(*) FROM matched m WHERE EXISTS(SELECT 1 FROM analytics_activity_days a
                WHERE a.user_id=m.user_id AND a.day BETWEEN $3 AND $4)""", lo, hi, start, end)
            report['profile_counts'] = await c.fetchrow("""SELECT count(*) AS total,
                count(*) FILTER(WHERE active AND NOT EXISTS(SELECT 1 FROM user_moderation m WHERE m.user_id=p.user_id AND blocked)) AS searchable
                FROM profiles p""")
    return report


async def users_report(db, query='', page=1, start=None, end=None):
    """All known users, optionally limited to observed activity on Moscow dates."""
    query = query.removeprefix('@')
    where = """WHERE ($1='' OR strpos(lower(concat_ws(' ',u.user_id::text,u.display_name,u.username)),lower($1))>0)
        AND ($2::date IS NULL OR EXISTS (SELECT 1 FROM analytics_activity_days a
            WHERE a.user_id=u.user_id AND a.day BETWEEN $2 AND $3))"""
    async with db.pool.acquire() as c:
        async with c.transaction(isolation='repeatable_read', readonly=True):
            total = await c.fetchval('SELECT count(*) FROM analytics_users u '+where, query,start,end)
            pages = max(1,(total+29)//30)
            page = min(max(1,page),pages)
            rows = await c.fetch("""SELECT u.*,p.active,coalesce(m.blocked,false) AS blocked,
                EXISTS(SELECT 1 FROM drafts d WHERE d.user_id=u.user_id) AS has_draft,
                EXISTS(SELECT 1 FROM profile_archives a WHERE a.user_id=u.user_id) AS has_archive
                FROM analytics_users u LEFT JOIN profiles p USING(user_id)
                LEFT JOIN user_moderation m USING(user_id) """+where+"""
                ORDER BY u.last_seen DESC NULLS LAST,u.user_id DESC LIMIT 30 OFFSET $4""",
                query,start,end,(page-1)*30)
    return {'total':total,'rows':rows,'page':page,'pages':pages}


async def user_history_report(db, user_id, start=None, end=None, pages=None):
    """A user's history, with independently paginated sections and a consistent snapshot."""
    pages = pages or {}
    lo = datetime.combine(start, time.min, MOSCOW) if start else None
    hi = datetime.combine(end+timedelta(days=1), time.min, MOSCOW) if end else None
    report = {'start':start, 'end':end}
    async with db.pool.acquire() as c:
        async with c.transaction(isolation='repeatable_read', readonly=True):
            user = await c.fetchrow("""SELECT u.*,p.active,coalesce(m.blocked,false) AS blocked,
                EXISTS(SELECT 1 FROM profile_archives a WHERE a.user_id=u.user_id) AS has_archive
                FROM analytics_users u LEFT JOIN profiles p USING(user_id)
                LEFT JOIN user_moderation m USING(user_id) WHERE u.user_id=$1""", user_id)
            if user is None:
                return None
            report['user'] = user
            report['tracking_since'] = await c.fetchval('SELECT applied_at FROM schema_migrations WHERE version=3')
            report['updates'] = await c.fetchrow("""SELECT count(*) AS total,
                count(*) FILTER(WHERE status='error') AS errors,
                count(*) FILTER(WHERE status='processing') AS processing,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) AS p95
                FROM analytics_updates WHERE user_id=$1
                AND ($2::timestamptz IS NULL OR received_at >= $2 AND received_at < $3)""", user_id,lo,hi)
            # The second participant also sees the match. Incoming likes are not their actions.
            event_where = """WHERE name<>'telegram_call'
                AND (user_id=$1 OR name='match_created' AND properties->>'target_id'=$1::bigint::text)
                AND ($2::timestamptz IS NULL OR occurred_at >= $2 AND occurred_at < $3)"""
            report['counts'] = await c.fetch('SELECT name,count(*) AS total FROM analytics_events '+event_where+' GROUP BY name',user_id,lo,hi)
            queries = {
                'attempts': ("""SELECT * FROM analytics_attempts WHERE user_id=$1
                    AND ($2::timestamptz IS NULL OR started_at >= $2 AND started_at < $3)""", 'started_at DESC,id DESC'),
                'events': ('SELECT * FROM analytics_events '+event_where, 'occurred_at DESC,id DESC'),
                'sessions': ("""SELECT * FROM analytics_sessions WHERE user_id=$1
                    AND ($2::timestamptz IS NULL OR started_at < $3 AND last_activity >= $2)""", 'started_at DESC,id DESC'),
                'errors': ("""SELECT update_id AS id,received_at AS at,'handler' AS kind,action AS operation,
                    error_type,duration_ms,session_id,NULL::bigint AS recipient_id,'bot' AS channel
                    FROM analytics_updates WHERE user_id=$1 AND status='error'
                    AND ($2::timestamptz IS NULL OR received_at >= $2 AND received_at < $3)
                    UNION ALL
                    SELECT id,occurred_at,'telegram',properties->>'method',properties->>'error_type',
                    (properties->>'duration_ms')::double precision,
                    CASE WHEN user_id=$1 THEN session_id END,
                    (properties->>'recipient_id')::bigint,properties->>'channel'
                    FROM analytics_events WHERE name='telegram_call' AND properties->>'ok'='false'
                    AND (user_id=$1 OR properties->>'recipient_id'=$1::bigint::text)
                    AND ($2::timestamptz IS NULL OR occurred_at >= $2 AND occurred_at < $3)""", 'at DESC,kind,id DESC'),
            }
            for key,(sql,order) in queries.items():
                total = await c.fetchval('SELECT count(*) FROM ('+sql+') records',user_id,lo,hi)
                max_page = max(1,(total+29)//30)
                current = min(max(1,pages.get(key,1)),max_page)
                rows = await c.fetch('SELECT * FROM ('+sql+') records ORDER BY '+order+' LIMIT 30 OFFSET $4',
                    user_id,lo,hi,(current-1)*30)
                report[key] = {'rows':rows,'total':total,'page':current,'pages':max_page}
    return report
