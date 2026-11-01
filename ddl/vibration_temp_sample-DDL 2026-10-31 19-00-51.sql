-- jurong_marine.vibration_temp_sample definition
CREATE TABLE `vibration_temp` (
  `id` int NOT NULL AUTO_INCREMENT,
  `sampled_at` datetime NOT NULL,
  `machine_id` varchar(10) NOT NULL,
  `vib_rms_mms` decimal(8,2) DEFAULT NULL,
  `temp` decimal(8,2) DEFAULT NULL,
  `temp_unit` varchar(10) DEFAULT NULL,
  `temp_c` decimal(8,2) DEFAULT NULL,
  `temp_f` decimal(8,2) DEFAULT NULL,
  `epoch_ms` bigint unsigned DEFAULT NULL,
  PRIMARY KEY (`id`)
);