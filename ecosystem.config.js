module.exports = {
  apps: [
    {
      name: "flashplan-bot",
      script: "main.py",
      interpreter: "venv/bin/python",
      cwd: "/root/AI/FlashPlan",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "/root/.pm2/logs/flashplan-bot-error.log",
      out_file: "/root/.pm2/logs/flashplan-bot-out.log",
      merge_logs: true,
    },
  ],
};
