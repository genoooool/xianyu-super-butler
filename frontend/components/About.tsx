import React from 'react';
import { Info } from 'lucide-react';
import { PageHeader } from './ui';
import SoftwareUpdate from './SoftwareUpdate';

const About: React.FC = () => (
  <div className="page-stack animate-fade-in">
    <PageHeader title="关于闲鱼工作台" description="本机客服、商品与订单管理。" icon={Info} />
    <SoftwareUpdate />
    <p className="px-2 text-xs leading-6 text-gray-500">
      本工作台基于开源项目持续开发。更新仅来自
      <a href="https://github.com/genoooool/xianyu-super-butler" target="_blank" rel="noopener noreferrer" className="mx-1 underline">genoooool/xianyu-super-butler</a>。
      <a href="https://github.com/23Star/xianyu-super-butler" target="_blank" rel="noopener noreferrer" className="ml-2 underline">原项目与贡献者</a>
    </p>
  </div>
);
export default About;
