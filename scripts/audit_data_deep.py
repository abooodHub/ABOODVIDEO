import subprocess

def run_remote_sql(sql: str):
    ssh_proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", 
         "docker exec -i aboodvideo_postgres psql -U aboodvideo_user -d aboodvideo_db -c \"\\x off\" -c \"" + sql + "\""],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    stdout, stderr = ssh_proc.communicate()
    if ssh_proc.returncode != 0:
        raise RuntimeError(f"SQL failed: {stderr}")
    return stdout

print("=== ALL TABLES IN DB ===")
print(run_remote_sql("""
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
ORDER BY table_name;
"""))

print("=== READY VIDEOS METADATA AUDIT ===")
print(run_remote_sql("""
SELECT 
    count(1) as total_ready,
    count(poster_path) as has_poster_path,
    count(CASE WHEN poster_path IS NOT NULL AND poster_path != '' THEN 1 END) as has_valid_poster_str,
    count(series_name) as has_series_name,
    count(season_number) as has_season_number,
    count(episode_number) as has_episode_number,
    count(category) as has_category,
    count(description) as has_description,
    count(source_duration_seconds) as has_duration
FROM videos
WHERE status = 'READY';
"""))

print("=== SAMPLE READY VIDEOS ===")
print(run_remote_sql("""
SELECT id, title, series_name, season_number, episode_number, category, poster_path, source_duration_seconds
FROM videos
WHERE status = 'READY'
ORDER BY created_at DESC
LIMIT 15;
"""))

print("=== RENDITIONS AND RESOLUTIONS OF READY VIDEOS ===")
print(run_remote_sql("""
SELECT 
    v.id,
    v.title,
    count(vr.id) as rendition_count,
    max(vr.resolution_height) as max_height,
    bool_or(vr.resolution_height = 2160) as has_2160p,
    bool_or(vr.resolution_height = 1440) as has_1440p,
    bool_or(vr.resolution_height = 1080) as has_1080p,
    bool_or(vr.resolution_height = 720) as has_720p
FROM videos v
LEFT JOIN video_renditions vr ON v.id = vr.video_id
WHERE v.status = 'READY'
GROUP BY v.id, v.title
ORDER BY max_height DESC NULLS LAST
LIMIT 15;
"""))

print("=== RESOLUTION COUNTS ACROSS ALL READY VIDEOS ===")
print(run_remote_sql("""
WITH per_video AS (
    SELECT 
        v.id,
        max(vr.resolution_height) as max_h,
        bool_or(vr.resolution_height = 2160) as has_2160,
        bool_or(vr.resolution_height = 1440) as has_1440,
        bool_or(vr.resolution_height = 1080) as has_1080
    FROM videos v
    LEFT JOIN video_renditions vr ON v.id = vr.video_id
    WHERE v.status = 'READY'
    GROUP BY v.id
)
SELECT 
    count(CASE WHEN max_h = 2160 THEN 1 END) as max_2160p,
    count(CASE WHEN max_h = 1440 THEN 1 END) as max_1440p,
    count(CASE WHEN max_h = 1080 THEN 1 END) as max_1080p,
    count(CASE WHEN max_h < 1080 THEN 1 END) as max_under_1080p,
    count(CASE WHEN max_h IS NULL THEN 1 END) as no_renditions
FROM per_video;
"""))
