# Single-host application VM for production.
#
# Deliberately a compose host rather than ECS/ALB: the application is one image plus Redis and a
# reverse proxy, and keeping the SAME compose topology in dev and prod is what makes "runs locally"
# and "deploys" the same story. Scaling out means adding hosts behind a load balancer, which is a
# different exercise.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Application host: HTTP/HTTPS public, SSH restricted"

  dynamic "ingress" {
    for_each = [80, 443]
    content {
      description = "Public web"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # No default SSH exposure. Pass an explicit CIDR when you need a shell; an empty list means the
  # port is closed, which is the right default for a machine that only serves HTTP.
  dynamic "ingress" {
    for_each = var.ssh_cidr_blocks
    content {
      description = "Operator SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    description = "All egress (image pulls, package installs, AWS APIs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-app" })
}

# The instance talks to S3 through its instance profile: no static keys on the box.
data "aws_iam_policy_document" "s3_access" {
  statement {
    sid     = "ReadWriteAppBucket"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]

    resources = ["${var.bucket_arn}/*"]
  }

  statement {
    sid     = "ListAppBucket"
    effect  = "Allow"
    actions = ["s3:ListBucket"]

    resources = [var.bucket_arn]
  }
}

resource "aws_iam_role" "app" {
  name        = "${var.name}-app"
  description = "Instance role for the ${var.name} application host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "s3_access" {
  name   = "s3-access"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.s3_access.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-app"
  role = aws_iam_role.app.name
  tags = var.tags
}

locals {
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    app_dir = var.app_dir
  })
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  iam_instance_profile   = aws_iam_instance_profile.app.name
  vpc_security_group_ids = [aws_security_group.app.id]
  user_data              = local.user_data
  tags                   = merge(var.tags, { Name = "${var.name}-app" })

  # The Redis volume and the staged images live on this root disk; replacing the instance loses them.
  # Immutable AMIs are the right answer in a real system — here, say it out loud instead.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }
}

resource "aws_eip" "app" {
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name}-app" })
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}
