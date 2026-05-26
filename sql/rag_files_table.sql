CREATE TABLE `rag_files` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `file_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '文件名',
  `file_uid` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '文件唯一值',
  `file_ext` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '文件扩展名',
  `file_size` int(11) DEFAULT 0 COMMENT '文件大小,单位：字节',
  `file_hash` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '文件的hash值',
  `original_path` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '原始文件存放路径',
  `parse_path` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT '' COMMENT '解析后的文件路径',
  `parse_status` tinyint(4) DEFAULT 0 COMMENT '解析文件的状态 -1-解析失败 0-未解析 1-正在解析 2-解析完成',
  `clean_status` tinyint(4) DEFAULT 0 COMMENT '清洗状态 -1-清洗失败 0-未清洗 1-正在清洗 2-已清洗',
  `created_at` int(11) DEFAULT 0 COMMENT '创建时间的时间戳',
  `updated_at` int(11) DEFAULT 0 COMMENT '更新时间的时间戳',
  PRIMARY KEY (`id`),
  KEY `file_hash` (`file_hash`),
  KEY `original_path` (`original_path`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
