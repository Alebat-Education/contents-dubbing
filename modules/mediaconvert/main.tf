resource "aws_media_convert_queue" "this" {
  name         = "${var.name_prefix}-queue"
  description  = "On-demand queue for the medical dubbing PoC packaging workflow."
  pricing_plan = "ON_DEMAND"
  status       = "ACTIVE"
  tags         = var.tags
}
