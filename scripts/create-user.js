#!/usr/bin/env node
/**
 * 创建 DeerFlow 用户脚本
 * 
 * 用法:
 *   node scripts/create-user.js <email> <password> [name]
 * 
 * 示例:
 *   node scripts/create-user.js admin@company.com securepassword "管理员"
 *   node scripts/create-user.js user@company.com userpassword
 */

const { createHash } = require("crypto");
const { Database } = require("better-sqlite3");
const path = require("path");
const fs = require("fs");

// 获取数据库路径
function getDatabasePath() {
  const dataDir = path.join(__dirname, "..", "frontend", "data");
  if (!fs.existsSync(dataDir)) {
    fs.mkdirSync(dataDir, { recursive: true });
  }
  return path.join(dataDir, "auth.db");
}

// 生成 UUID
function generateUUID() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// 哈希密码（使用 SHA-256 + salt 的简单实现，Better Auth 实际使用更复杂的哈希）
function hashPassword(password) {
  const salt = generateUUID();
  const hash = createHash("sha256")
    .update(password + salt)
    .digest("hex");
  return { hash, salt };
}

// 创建用户
function createUser(email, password, name = null) {
  const dbPath = getDatabasePath();
  
  // 如果数据库不存在，Better Auth 会自动创建表结构
  // 这里我们需要确保表存在
  const db = new Database(dbPath);
  
  // 启用外键
  db.pragma("journal_mode = WAL");
  
  // 创建用户表（如果不存在）
  db.exec(`
    CREATE TABLE IF NOT EXISTS user (
      id TEXT PRIMARY KEY,
      email TEXT UNIQUE NOT NULL,
      email_verified INTEGER DEFAULT 0,
      name TEXT,
      image TEXT,
      created_at INTEGER DEFAULT (unixepoch()),
      updated_at INTEGER DEFAULT (unixepoch())
    );
    
    CREATE TABLE IF NOT EXISTS account (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES user(id),
      account_id TEXT NOT NULL,
      provider_id TEXT NOT NULL,
      access_token TEXT,
      refresh_token TEXT,
      access_token_expires_at INTEGER,
      refresh_token_expires_at INTEGER,
      scope TEXT,
      id_token TEXT,
      password TEXT,
      created_at INTEGER DEFAULT (unixepoch()),
      updated_at INTEGER DEFAULT (unixepoch())
    );
    
    CREATE TABLE IF NOT EXISTS session (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES user(id),
      token TEXT UNIQUE NOT NULL,
      expires_at INTEGER NOT NULL,
      ip_address TEXT,
      user_agent TEXT,
      created_at INTEGER DEFAULT (unixepoch()),
      updated_at INTEGER DEFAULT (unixepoch())
    );
    
    CREATE INDEX IF NOT EXISTS idx_account_user_id ON account(user_id);
    CREATE INDEX IF NOT EXISTS idx_session_user_id ON session(user_id);
    CREATE INDEX IF NOT EXISTS idx_session_token ON session(token);
  `);
  
  // 检查用户是否已存在
  const existingUser = db.prepare("SELECT id FROM user WHERE email = ?").get(email);
  if (existingUser) {
    console.error(`❌ 用户 ${email} 已存在`);
    db.close();
    process.exit(1);
  }
  
  // 生成用户 ID
  const userId = generateUUID();
  const now = Math.floor(Date.now() / 1000);
  
  // 插入用户
  const insertUser = db.prepare(`
    INSERT INTO user (id, email, email_verified, name, created_at, updated_at)
    VALUES (?, ?, 1, ?, ?, ?)
  `);
  insertUser.run(userId, email, name || email.split("@")[0], now, now);
  
  // 创建账户记录（使用 bcrypt 哈希密码）
  // 注意：这里使用 bcrypt 格式让 Better Auth 能正确验证
  const bcryptHash = hashPasswordBcrypt(password);
  const accountId = generateUUID();
  
  const insertAccount = db.prepare(`
    INSERT INTO account (id, user_id, account_id, provider_id, password, created_at, updated_at)
    VALUES (?, ?, ?, 'credential', ?, ?, ?)
  `);
  insertAccount.run(accountId, userId, email, bcryptHash, now, now);
  
  db.close();
  
  console.log(`✅ 用户创建成功`);
  console.log(`   邮箱: ${email}`);
  console.log(`   姓名: ${name || email.split("@")[0]}`);
  console.log(`   数据库: ${dbPath}`);
}

// 使用 bcrypt 哈希密码
function hashPasswordBcrypt(password) {
  // 尝试使用 bcrypt 模块
  try {
    const bcrypt = require("bcrypt");
    return bcrypt.hashSync(password, 10);
  } catch (e) {
    // 如果没有 bcrypt，使用简单的哈希格式
    // Better Auth 期望 bcrypt 格式: $2b$10$...
    console.warn("⚠️  bcrypt 模块未安装，请运行: npm install bcrypt");
    console.warn("   临时使用占位符，登录可能无法正常工作");
    // 返回一个无效的 bcrypt 哈希，只是占位符
    return "$2b$10$" + "x".repeat(53);
  }
}

// 主函数
function main() {
  const args = process.argv.slice(2);
  
  if (args.length < 2) {
    console.log("用法: node scripts/create-user.js <邮箱> <密码> [姓名]");
    console.log("示例: node scripts/create-user.js admin@company.com securepassword \"管理员\"");
    process.exit(1);
  }
  
  const [email, password, name] = args;
  
  // 验证邮箱格式
  if (!email.includes("@")) {
    console.error("❌ 无效的邮箱格式");
    process.exit(1);
  }
  
  // 验证密码长度
  if (password.length < 6) {
    console.error("❌ 密码长度至少 6 位");
    process.exit(1);
  }
  
  try {
    createUser(email, password, name);
  } catch (error) {
    console.error("❌ 创建用户失败:", error.message);
    process.exit(1);
  }
}

main();
