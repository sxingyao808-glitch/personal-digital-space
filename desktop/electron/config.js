'use strict';

/**
 * Data directory resolution for the desktop build.
 *
 * The install directory never participates: user data always resolves to an
 * explicit, user-owned location so installing, upgrading or uninstalling the
 * program cannot touch the archive.
 */

const fs = require('node:fs');
const path = require('node:path');

const CONFIG_FILENAME = 'desktop-config.json';
const ENV_VAR = 'ARCHIVE_DATA_DIR';

class DataDirError extends Error {
  constructor(title, message, detail = '') {
    super(message);
    this.name = 'DataDirError';
    this.title = title;
    this.detail = detail;
  }
}

function isInside(child, parent) {
  const relative = path.relative(parent, child);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

/**
 * @param {object} options
 * @param {Record<string, string|undefined>} options.env
 * @param {string} options.configPath
 * @param {string} options.defaultDataDir
 * @param {string} [options.installRoot]
 * @returns {{dataDir: string, source: string, configPath: string, notice: string}}
 */
function resolveDataDir({ env, configPath, defaultDataDir, installRoot }) {
  let dataDir = defaultDataDir;
  let source = 'default';
  let notice = '';

  const fromEnv = (env[ENV_VAR] || '').trim();
  if (fromEnv) {
    dataDir = fromEnv;
    source = 'env';
  } else if (fs.existsSync(configPath)) {
    let parsed;
    try {
      // A hand-edited file may start with a BOM; that is not invalid JSON.
      const raw = fs.readFileSync(configPath, 'utf8').replace(/^\uFEFF/, '');
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new DataDirError(
        '数据目录配置无法解析',
        `配置文件 ${CONFIG_FILENAME} 不是有效的 JSON，程序不会猜测数据位置。`,
        String(error && error.message)
      );
    }
    const configured = parsed && typeof parsed.dataDir === 'string' ? parsed.dataDir.trim() : '';
    if (!configured) {
      throw new DataDirError(
        '数据目录配置缺少 dataDir',
        `配置文件 ${CONFIG_FILENAME} 中没有可用的 "dataDir" 字段。`
      );
    }
    dataDir = configured;
    source = 'config';
  }

  if (!path.isAbsolute(dataDir)) {
    throw new DataDirError(
      '数据目录必须是绝对路径',
      `解析到的数据目录不是绝对路径：${dataDir}`
    );
  }
  dataDir = path.resolve(dataDir);

  if (installRoot && isInside(dataDir, path.resolve(installRoot))) {
    throw new DataDirError(
      '数据目录不能位于安装目录内',
      '用户数据必须与程序文件分离，否则安装、升级或卸载可能损坏档案。',
      `数据目录：${dataDir}\n安装目录：${installRoot}`
    );
  }

  if (source === 'default') {
    notice = '当前使用桌面应用的默认数据目录。';
  }

  // An explicitly configured directory that is gone is a mistake, not a fresh
  // start: refusing keeps a moved or renamed archive from silently turning into
  // an empty one. A brand-new archive simply omits the config file.
  if ((source === 'config' || source === 'env') && !fs.existsSync(dataDir)) {
    throw new DataDirError(
      '数据目录不存在',
      `指定的数据目录不存在：${dataDir}`,
      '请检查路径是否正确，或在创建新档案时不要指定该目录。'
    );
  }

  return { dataDir, source, configPath, notice };
}

module.exports = { resolveDataDir, DataDirError, CONFIG_FILENAME, ENV_VAR };
