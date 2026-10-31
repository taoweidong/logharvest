# LogHarvest 定时备份镜像：基于 postgres:18.6（生产镜像白名单基座）。
# pg_dump 与数据库服务器版本严格一致（pg_dump 客户端必须 >= 服务器大版本），
# 替代第三方 postgres-backup-local 镜像——生产环境只允许使用清单内基础镜像本地构建。
FROM postgres:18.6

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

ENTRYPOINT ["/usr/local/bin/backup.sh"]
