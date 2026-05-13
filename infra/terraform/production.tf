################################################################################
# Production additions: RDS, ElastiCache, ALB, IAM, Secrets, ECS Services
# Reference architecture for miniblog. Apply on top of existing main.tf.
#
# Cost estimate (us-east-1, single-AZ minimal): ~$60-80/month
#   - Fargate API + Worker (2x ea, 0.5 vCPU): ~$30
#   - RDS db.t3.micro single-AZ:               ~$13
#   - ElastiCache cache.t3.micro single node:  ~$12
#   - ALB:                                     ~$20
#   - NAT Gateway (single AZ, in main.tf):     ~$32  ← largest fixed cost
#   - S3 / ECR / CloudWatch / data transfer:    ~$5
################################################################################

# -----------------------------------------------------------------------------
# Security Groups — least-privilege between tiers
# -----------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "miniblog-alb-sg"
  description = "ALB ingress 80/443 from internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]   # 用于 HTTP -> HTTPS redirect
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs_task" {
  name        = "miniblog-ecs-task-sg"
  description = "ECS tasks: ingress only from ALB; full egress for upstream APIs"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]   # 出网调 OpenAI / S3 / RDS / Redis
  }
}

resource "aws_security_group" "rds" {
  name        = "miniblog-rds-sg"
  description = "RDS: only ECS tasks can connect"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_task.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "miniblog-redis-sg"
  description = "Redis: only ECS tasks can connect"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_task.id]
  }
}

# -----------------------------------------------------------------------------
# RDS MySQL — single-AZ to start, enable multi_az = true for production HA
# -----------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = "miniblog-db-subnet"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "mysql" {
  identifier              = "miniblog-mysql"
  engine                  = "mysql"
  engine_version          = "8.0"
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true

  db_name                 = "miniblog"
  username                = "admin"
  # 真正的密码由 Secrets Manager 管理；这里用 manage_master_user_password
  manage_master_user_password = true

  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  publicly_accessible     = false
  multi_az                = false   # 生产 HA 改 true,~2x 成本

  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  skip_final_snapshot     = true    # 生产改 false
  deletion_protection     = false   # 生产改 true

  tags = { Name = "miniblog-mysql" }
}

# -----------------------------------------------------------------------------
# ElastiCache Redis
# -----------------------------------------------------------------------------

resource "aws_elasticache_subnet_group" "main" {
  name       = "miniblog-redis-subnet"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "miniblog-redis"
  engine               = "redis"
  engine_version       = "7.0"
  node_type            = "cache.t3.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  tags = { Name = "miniblog-redis" }
}

# -----------------------------------------------------------------------------
# Secrets Manager — API keys never in image / env / Task Definition plain text
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "app_secrets" {
  name = "miniblog/app-secrets"
}

resource "aws_secretsmanager_secret_version" "app_secrets" {
  secret_id = aws_secretsmanager_secret.app_secrets.id
  secret_string = jsonencode({
    OPENAI_API_KEY    = "REPLACE_ME"
    ELEVENSLAB_API_KEY = "REPLACE_ME"
    JWT_SECRET_KEY    = "REPLACE_ME"
  })

  lifecycle {
    ignore_changes = [secret_string]   # 创建后通过 AWS console 改值,terraform 不再管
  }
}

# -----------------------------------------------------------------------------
# IAM — execution role (拉镜像/写日志/读 secret),  task role (业务调 S3 之类)
# -----------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "miniblog-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "read-secrets"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.app_secrets.arn,
        aws_db_instance.mysql.master_user_secret[0].secret_arn,
      ]
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name               = "miniblog-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "ecs_task_s3" {
  name = "s3-storage"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      Resource = [
        aws_s3_bucket.storage.arn,
        "${aws_s3_bucket.storage.arn}/*",
      ]
    }]
  })
}

# -----------------------------------------------------------------------------
# ALB + Target Group + Listener
# 想接 HTTPS 需要 ACM 证书+域名,先注释起来,获得证书后取消注释
# -----------------------------------------------------------------------------

resource "aws_lb" "main" {
  name               = "miniblog-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "miniblog-alb" }
}

resource "aws_lb_target_group" "api" {
  name        = "miniblog-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"   # Fargate 必须用 ip 模式

  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }
}

# 起步先用 HTTP,有域名后再加 HTTPS listener + redirect
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# 生产 HTTPS listener — 取消注释并填入 ACM cert ARN
# resource "aws_lb_listener" "https" {
#   load_balancer_arn = aws_lb.main.arn
#   port              = 443
#   protocol          = "HTTPS"
#   ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
#   certificate_arn   = "arn:aws:acm:us-east-1:xxx:certificate/yyy"
#   default_action {
#     type             = "forward"
#     target_group_arn = aws_lb_target_group.api.arn
#   }
# }

# -----------------------------------------------------------------------------
# ECS Task Definitions + Services — API 和 Worker 同镜像不同 command
# -----------------------------------------------------------------------------

locals {
  app_secrets_arn = aws_secretsmanager_secret.app_secrets.arn
  rds_secret_arn  = aws_db_instance.mysql.master_user_secret[0].secret_arn
}

resource "aws_ecs_task_definition" "api" {
  family                   = "miniblog-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.agent.repository_url}:latest"
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [
      { name = "APP_ENV",         value = "production" },
      { name = "REDIS_HOST",      value = aws_elasticache_cluster.redis.cache_nodes[0].address },
      { name = "REDIS_PORT",      value = "6379" },
      { name = "MYSQL_HOST",      value = aws_db_instance.mysql.address },
      { name = "S3_BUCKET",       value = aws_s3_bucket.storage.id },
    ]

    # 敏感信息从 Secrets Manager 注入,不走 environment
    secrets = [
      { name = "OPENAI_API_KEY",    valueFrom = "${local.app_secrets_arn}:OPENAI_API_KEY::" },
      { name = "ELEVENSLAB_API_KEY", valueFrom = "${local.app_secrets_arn}:ELEVENSLAB_API_KEY::" },
      { name = "JWT_SECRET_KEY",    valueFrom = "${local.app_secrets_arn}:JWT_SECRET_KEY::" },
      { name = "MYSQL_PASSWORD",    valueFrom = "${local.rds_secret_arn}:password::" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ecs.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 2
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
}

# Celery Worker —— 同镜像不同 command,无需 ALB 关联
resource "aws_ecs_task_definition" "worker" {
  family                   = "miniblog-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.agent.repository_url}:latest"
    essential = true
    command   = ["python", "celery_worker.py"]   # ← 关键:覆盖 Dockerfile CMD

    environment = [
      { name = "APP_ENV",    value = "production" },
      { name = "REDIS_HOST", value = aws_elasticache_cluster.redis.cache_nodes[0].address },
      { name = "MYSQL_HOST", value = aws_db_instance.mysql.address },
      { name = "S3_BUCKET",  value = aws_s3_bucket.storage.id },
    ]

    secrets = [
      { name = "OPENAI_API_KEY",    valueFrom = "${local.app_secrets_arn}:OPENAI_API_KEY::" },
      { name = "ELEVENSLAB_API_KEY", valueFrom = "${local.app_secrets_arn}:ELEVENSLAB_API_KEY::" },
      { name = "MYSQL_PASSWORD",    valueFrom = "${local.rds_secret_arn}:password::" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ecs.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 2
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = false
  }
}

# -----------------------------------------------------------------------------
# Outputs — apply 完直接看关键地址
# -----------------------------------------------------------------------------

output "alb_dns" {
  value       = aws_lb.main.dns_name
  description = "ALB DNS name — 改 Route53 CNAME 指向这里"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "rds_endpoint" {
  value     = aws_db_instance.mysql.address
  sensitive = true
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}
