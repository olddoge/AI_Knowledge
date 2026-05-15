-- 清洗 Markdown 时抽取到的图片引用表
CREATE TABLE `rag_image` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `file_uid` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT '' COMMENT '源文件唯一值',
  `image_file` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT '' COMMENT '图片文件名',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_file_uid_image_file` (`file_uid`, `image_file`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
