-- idp_sim.sql —— 「公司统一身份源（UM/KIP/SSO）」的测试替身
--
-- 架构定位（v2/C 路线）：
--   生产环境的员工账号由公司身份体系管理，门户与 WeKnora 都是消费方。
--   本脚本在测试库里模拟那个身份源：portal-sim 只读本表做登录校验
--   （工号 + bcrypt 密码），认证通过后经 WeKnora bridge 换票——
--   本表不参与任何授权判断（README 纪律 1）。
--
-- 数据来源：从 B1 时代的 portal_proxy.employees 原样迁出（bcrypt 哈希
-- 未变，密码仍是 review123456）。portal_proxy 库属已冻结的 B1 实物，
-- 保留不动，本库是 C 路线自己的测试夹具。
--
-- 用法（针对 WeKnora-postgres-dev 容器）：
--   sudo docker exec -i WeKnora-postgres-dev psql -U postgres < fixtures/idp_sim.sql
-- 脚本可重复执行（库/表存在则跳过，账号按工号幂等插入）。

-- 1) 建库（存在则跳过）
SELECT 'CREATE DATABASE idp_sim'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'idp_sim')\gexec

\c idp_sim

-- 2) 建表
CREATE TABLE IF NOT EXISTS public.employees (
    id            bigserial PRIMARY KEY,
    uum_user_id   varchar(128) NOT NULL UNIQUE,
    display_name  varchar(128) NOT NULL,
    password_hash varchar(128) NOT NULL,          -- bcrypt
    is_admin      boolean      NOT NULL DEFAULT false,  -- B1 遗留字段，C 路线忽略（C 的管理员=WeKnora 系统管理员）
    is_active     boolean      NOT NULL DEFAULT true,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

-- 3) 测试账号（密码统一 review123456，哈希自 portal_proxy 原样迁出）
INSERT INTO public.employees (uum_user_id, display_name, password_hash, is_admin, is_active) VALUES
  ('REVIEW-A0001', '评审管理员', '$2b$10$V/DQivIOltD4jFTBTlhRgeoxIE/qshv50hEFtEydVg/4iok0jR3Na', true,  true),
  ('REVIEW-U0001', '评审用户一', '$2b$10$BYqPoldGjaSkZWwiKjxRK.O9lh/6yIOB.QWIGX8NmB56DFwmb/s8q', false, true),
  ('REVIEW-U0002', '评审用户二', '$2b$10$L.yykrLVdSV7ZStKg/Tfded/epQUw.SP7ClM5JzRjyD7chaRvEoLe', false, true),
  ('REVIEW-U0003', '评审用户三', '$2b$10$36LTVSkwZarRq.GkZhjCZeMflakJD2lCknMSK3A./ts8Kha9hH23.', false, true)
ON CONFLICT (uum_user_id) DO NOTHING;

-- 账号语义（授权在 WeKnora 侧，与本表无关）：
--   REVIEW-U0001：个人 owner + 团队 contributor + 公共库 admin（主力演示账号）
--   REVIEW-U0002：团队 viewer + 公共库经组织共享（受限对照）
--   REVIEW-A0001 / REVIEW-U0003：无任何授权（冷启动 / 预授权演示对象）
--   管理台账号 review-v2-admin@test.local 是 WeKnora 系统管理员，不在此表
