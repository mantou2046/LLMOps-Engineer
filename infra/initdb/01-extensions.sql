-- =============================================================================
-- 初始化扩展：pgvector
-- -----------------------------------------------------------------------------
-- ⚠️ 执行时机：只在「数据卷为空」的首次启动执行一次。
--    改了本文件想重跑：docker compose -f infra/compose.yml down -v && up -d
--    （down -v 会删数据，本地开发无所谓；生产环境用迁移工具，不靠 initdb）
--
-- 文件被挂载到 /docker-entrypoint-initdb.d/，官方 postgres 镜像会按文件名
-- 字典序执行其中的 .sql / .sh。01- 前缀留出后续扩位空间。
-- =============================================================================

-- pgvector：向量类型与相似度检索
-- 之所以要显式 CREATE EXTENSION，是因为 pgvector/pgvector 镜像只是「自带」该扩展，
-- 并非默认启用 —— 不建的话 `CREATE TABLE ... vector(1536)` 会直接报类型不存在。
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm：三元组模糊匹配，W6 做「混合检索」（向量 + 关键词）时会用到。
-- 制度问答里专有名词（如「银行承兑汇票」）靠向量召回不稳，关键词侧能兜住。
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent：忽略重音，主要用于英文术语，锦上添花
CREATE EXTENSION IF NOT EXISTS unaccent;

-- 确认结果（初始化日志里能看到）
DO $$
BEGIN
    RAISE NOTICE '已启用扩展: %',
        (SELECT string_agg(extname, ', ' ORDER BY extname)
           FROM pg_extension
          WHERE extname IN ('vector', 'pg_trgm', 'unaccent'));
END $$;
