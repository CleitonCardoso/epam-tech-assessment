output "instance_id" {
  description = "EC2 instance id of the application host."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Elastic IP of the application host."
  value       = aws_eip.app.public_ip
}

output "security_group_id" {
  description = "Security group attached to the host (useful when opening SSH temporarily)."
  value       = aws_security_group.app.id
}

output "instance_role_arn" {
  description = "Instance profile role ARN — the host uses it for S3 access, with no static keys."
  value       = aws_iam_role.app.arn
}

output "ssh_command" {
  description = "Ready-to-use SSH command. Only valid when ssh_cidr_blocks is non-empty."
  value       = "ssh ec2-user@${aws_eip.app.public_ip}"
}
