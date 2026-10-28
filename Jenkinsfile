// LogHarvest Jenkins 流水线：质量门禁 -> 镜像构建推送 -> 升级前备份归档 -> 三栈发布（数据库/控制面/Worker）-> 冒烟验证。
//
// 部署形态：三个独立 compose 栈（deploy/{postgres,server,worker}），通过外部网络
// logharvest-net 互通；所有数据归档在数据盘 /usr1/logharvest/（见 deploy/init-dirs.sh）。
//
// 依赖的 Jenkins 凭据（Manage Jenkins -> Credentials）：
//   LOGHARVEST_REGISTRY_CRED  Secret text：镜像仓库地址（如 registry.example.com）
//   REGISTRY_USER / REGISTRY_PASSWORD   UsernamePassword：镜像仓库登录
//   DEPLOY_HOST              Secret text：发布目标主机地址
//   DEPLOY_SSH_KEY           SSH Username with private key：发布主机私钥（用户 deploy）
// 目标主机要求：Docker + Compose v2；仓库检出在 /opt/logharvest；
//   deploy/{postgres,server,worker}/.env 已按 *.env.example 配置（数据目录默认 /usr1）。

pipeline {
    agent any

    options {
        timestamps()
        ansiColor('xterm')
        buildDiscarder(logRotator(numToKeepStr: '30'))
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
    }

    parameters {
        string(name: 'IMAGE_TAG', defaultValue: "build-${BUILD_NUMBER}", description: '镜像标签')
        string(name: 'WORKER_SCALE', defaultValue: '4', description: 'Worker 副本数（4-6）')
        booleanParam(name: 'BACKUP_BEFORE_DEPLOY', defaultValue: true,
                     description: '发布前执行 pg_dump 归档到 /usr1/logharvest/postgres/backup')
        booleanParam(name: 'DEPLOY', defaultValue: false, description: '测试通过后是否自动发布')
    }

    environment {
        // 与 agent.md 的测试门禁一致：任何一步红即终止，不产出镜像
        COMPOSE = 'docker compose'
        // Secret text：镜像仓库地址（如 registry.example.com）与发布目标主机
        LOGHARVEST_REGISTRY = credentials('LOGHARVEST_REGISTRY_CRED')
        DEPLOY_HOST = credentials('DEPLOY_HOST')
        APP = 'docker compose -f deploy/server/docker-compose.yml --env-file deploy/server/.env'
        DB = 'docker compose -f deploy/postgres/docker-compose.yml --env-file deploy/postgres/.env'
        WK = 'docker compose -f deploy/worker/docker-compose.yml --env-file deploy/worker/.env'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                sh 'git rev-parse --short HEAD > .git-short'
            }
        }

        stage('后端静态检查') {
            steps {
                dir('service') {
                    sh 'uv sync --python 3.12 --frozen'
                    sh 'uv run ruff check src tests scripts'
                    sh 'uv run ruff format --check src tests'
                    sh 'uv run mypy'
                }
            }
        }

        stage('后端单元测试') {
            steps {
                dir('service') {
                    // 单元套件不需要真实 PostgreSQL；PG 专有 SQL 由下一阶段的一次性容器覆盖。
                    sh 'uv run pytest tests -q'
                }
            }
        }

        stage('PostgreSQL 集成测试') {
            steps {
                dir('service') {
                    // 空库 alembic upgrade head + file_tasks 索引断言 + tests/integration；
                    // 脚本自带一次性容器、端口探测与退出清理，不接触部署数据库。
                    sh 'bash scripts/run_pg_integration.sh'
                }
            }
        }

        stage('前端测试') {
            steps {
                dir('web') {
                    sh 'corepack enable && pnpm install --frozen-lockfile'
                    sh 'pnpm test'
                    sh 'pnpm typecheck'
                }
            }
        }

        stage('构建镜像') {
            steps {
                sh '''
                  docker build -f service/Dockerfile --target server -t "$LOGHARVEST_REGISTRY/logharvest-server:$IMAGE_TAG" ./service
                  docker build -f service/Dockerfile --target worker -t "$LOGHARVEST_REGISTRY/logharvest-worker:$IMAGE_TAG" ./service
                  docker build -f web/Dockerfile        -t "$LOGHARVEST_REGISTRY/logharvest-web:$IMAGE_TAG"    ./web
                '''
            }
        }

        stage('推送镜像') {
            when { expression { params.DEPLOY } }
            steps {
                withCredentials([usernamePassword(credentialsId: 'REGISTRY_PASSWORD',
                                                  usernameVariable: 'REG_USER',
                                                  passwordVariable: 'REG_PASS')]) {
                    sh 'echo "$REG_PASS" | docker login "$LOGHARVEST_REGISTRY" -u "$REG_USER" --password-stdin'
                    sh '''
                      docker push "$LOGHARVEST_REGISTRY/logharvest-server:$IMAGE_TAG"
                      docker push "$LOGHARVEST_REGISTRY/logharvest-worker:$IMAGE_TAG"
                      docker push "$LOGHARVEST_REGISTRY/logharvest-web:$IMAGE_TAG"
                    '''
                }
            }
        }

        stage('发布 0：目录与网络引导（幂等）') {
            when { expression { params.DEPLOY } }
            steps {
                // 数据盘目录规划（/usr1/logharvest/{postgres/{data,backup},worker/tmp,server/logs}）
                // 与跨栈外部网络 logharvest-net；已存在时全部跳过
                sshagent(credentials: ['DEPLOY_SSH_KEY']) {
                    sh 'ssh deploy@$DEPLOY_HOST "cd /opt/logharvest && git pull --ff-only && sudo bash deploy/init-dirs.sh"'
                }
            }
        }

        stage('发布 1：升级前数据库备份归档') {
            when { expression { params.DEPLOY && params.BACKUP_BEFORE_DEPLOY } }
            steps {
                // pg_dump 归档到数据盘 /usr1/logharvest/postgres/backup（与定时备份同目录）
                sshagent(credentials: ['DEPLOY_SSH_KEY']) {
                    sh '''
                      ssh deploy@$DEPLOY_HOST "
                        STAMP=\\$(date +%Y%m%d-%H%M%S);
                        docker exec logharvest-postgres pg_dump -U logharvest -d logharvest \\
                          | gzip > /usr1/logharvest/postgres/backup/pre-deploy-\\$STAMP.sql.gz;
                        ls -lh /usr1/logharvest/postgres/backup | tail -5
                      "
                    '''
                }
            }
        }

        stage('发布 2：数据库栈（确保运行并 healthy）') {
            when { expression { params.DEPLOY } }
            steps {
                // 数据库独立部署：仅拉取/校准，不做破坏性操作；迁移由 server 启动时执行。
                // compose up --wait 会阻塞到 logharvest-postgres 的健康检查变 healthy
                sshagent(credentials: ['DEPLOY_SSH_KEY']) {
                    sh '''
                      ssh deploy@$DEPLOY_HOST "
                        cd /opt/logharvest &&
                        $DB up -d --wait &&
                        docker inspect --format 'postgres health: {{.State.Health.Status}}' logharvest-postgres
                      "
                    '''
                }
            }
        }

        stage('发布 3：控制面栈（server + web）') {
            when { expression { params.DEPLOY } }
            steps {
                // 无状态直换；server 启动即执行 alembic upgrade head + RBAC 种子
                sshagent(credentials: ['DEPLOY_SSH_KEY']) {
                    sh '''
                      ssh deploy@$DEPLOY_HOST "
                        cd /opt/logharvest &&
                        IMAGE_TAG=$IMAGE_TAG $APP pull server web &&
                        IMAGE_TAG=$IMAGE_TAG $APP up -d server web
                      "
                    '''
                }
            }
        }

        stage('发布 4：Worker 栈（滚动换新 + 扩容）') {
            when { expression { params.DEPLOY } }
            steps {
                // Worker 换新前由 drain 语义兜底：强杀场景下 recover_stale 在 120s 内
                // 回收任务交给新副本（租约栅栏保证旧副本迟到写入失效）
                sshagent(credentials: ['DEPLOY_SSH_KEY']) {
                    sh '''
                      ssh deploy@$DEPLOY_HOST "
                        cd /opt/logharvest &&
                        IMAGE_TAG=$IMAGE_TAG $WK pull worker &&
                        IMAGE_TAG=$IMAGE_TAG $WK up -d --no-deps --scale worker=$WORKER_SCALE worker
                      "
                    '''
                }
            }
        }

        stage('冒烟验证') {
            when { expression { params.DEPLOY } }
            steps {
                sh 'curl -fsS "http://$DEPLOY_HOST:${SERVER_PORT:-8000}/api/health"'
                sh 'curl -fsS "http://$DEPLOY_HOST:${WEB_PORT:-80}/"'
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'service/**/junit*.xml, web/**/*.xml'
            cleanWs()
        }
        failure {
            echo '构建或测试失败：按 agent.md 门禁，禁止发布；镜像未被推送（除已推送阶段外的失败）'
        }
    }
}
